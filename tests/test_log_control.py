import copy
import json
import unittest
from unittest.mock import patch

import app as module
from fixtures import authenticated_client
from fixtures import healthy_deployment


class LogControlTests(unittest.TestCase):
    def setUp(self):
        self.client = authenticated_client(module.app)
        self.client.environ_base['HTTP_X_FINACLE_NAMESPACE'] = 'pre-uat-finacle'
        self.config = {'metadata': {'name': 'fnhttp-ac-env', 'resourceVersion': '12'},
                       'data': {'UNRELATED': 'keep', 'DEBUG_LOG_LEVEL': 'NONE'}}
        self.deployment, self.pod, self.replicaset = healthy_deployment()
        self.body = {'configmap': 'fnhttp-ac-env', 'mode': 'DEBUG', 'username': 'alice'}
        self.calls = []
        self.fail = None
        self.dry_fail = None
        self.oc_mock = patch.object(module, 'oc', side_effect=self.oc).start()
        self.addCleanup(patch.stopall)

    def oc(self, *args):
        self.calls.append(args)
        if args[:2] == ('get', 'configmap'):
            return json.dumps(self.config)
        if args[:2] == ('get', 'deployments'):
            return json.dumps({'items': [self.deployment] if self.deployment else []})
        if args[:2] == ('get', 'pods'):
            return json.dumps({'items': [self.pod]})
        if args[:2] == ('get', 'replicasets'):
            return json.dumps({'items': [self.replicaset]})
        if args[:2] == ('get', 'configmaps'):
            return json.dumps({'items': [self.config]})
        if args[0] == 'patch':
            dry_run = '--dry-run=server' in args
            if (self.dry_fail if dry_run else self.fail) == args[1]:
                raise module.ClusterError('forbidden')
            return '{}'
        raise AssertionError(args)

    def post(self, action, body=None):
        return self.client.post('/api/log-control/' + action, json=self.body if body is None else body,
                                headers={'X-Requested-With': 'FinacleSupport'})

    def confirmed(self):
        response = self.post('preview')
        self.assertEqual(response.status_code, 200)
        return {**self.body, 'confirmed': True, 'fingerprint': response.json['fingerprint']}

    def test_exact_requested_presets(self):
        self.assertEqual(module.logging_values('FNHTTP', 'DEBUG', 'alice'), {
            'FINACLE_LOG4FIN_ENABLED': 'N', 'DEBUG_LOG_USERS': 'alice',
            'DEBUG_LOG_LEVEL': 'DEBUG', 'GLOBAL_TRACE': 'OFF', 'TBA_DEBUG_LEVEL': '0'})
        self.assertEqual(module.logging_values('FNHTTP', 'TRACE', 'ALL'), {
            'FINACLE_LOG4FIN_ENABLED': 'Y', 'DEBUG_LOG_USERS': 'ALL',
            'DEBUG_LOG_LEVEL': 'TRACE', 'GLOBAL_TRACE': 'ON'})
        self.assertEqual(module.logging_values('FNHTTP', 'NORMAL', ''), {
            'FINACLE_LOG4FIN_ENABLED': 'N', 'DEBUG_LOG_USERS': 'NONE',
            'DEBUG_LOG_LEVEL': 'NONE', 'GLOBAL_TRACE': 'OFF'})
        for group in ('FIP', 'Finbranch'):
            self.assertEqual(module.logging_values(group, 'DEBUG', ''), {'FI_LOGLEVEL': 'DEBUG', 'LOG_LEVEL': 'DEBUG'})
            self.assertEqual(module.logging_values(group, 'NORMAL', ''), {'FI_LOGLEVEL': 'NONE', 'LOG_LEVEL': 'WARN'})
            with self.assertRaises(ValueError):
                module.logging_values(group, 'TRACE', 'ALL')

    def test_preview_only_dry_runs_and_apply_preserves_other_keys(self):
        body = self.confirmed()
        self.assertTrue(all(c[0] == 'get' or '--dry-run=server' in c for c in self.calls))
        self.assertEqual(self.post('apply', body).status_code, 200)
        mutations = [c for c in self.calls if c[0] == 'patch' and '--dry-run=server' not in c]
        self.assertEqual([c[1:3] for c in mutations], [('configmap', 'fnhttp-ac-env'), ('deployment', 'actual-ac')])
        config_patch = json.loads(mutations[0][-1])
        self.assertEqual(config_patch[0], {'op': 'test', 'path': '/metadata/resourceVersion', 'value': '12'})
        self.assertEqual(config_patch[1]['value']['UNRELATED'], 'keep')
        self.assertEqual(config_patch[1]['value']['DEBUG_LOG_USERS'], 'alice')
        deployment_patch = json.loads(mutations[1][-1])
        self.assertEqual(deployment_patch['metadata']['resourceVersion'], '31')
        self.assertIn('kubectl.kubernetes.io/restartedAt', deployment_patch['spec']['template']['metadata']['annotations'])

    def test_stale_preview_blocks_all_writes(self):
        for target in ('config', 'deployment'):
            with self.subTest(target=target):
                body = self.confirmed()
                getattr(self, target)['metadata']['resourceVersion'] += '1'
                self.assertEqual(self.post('apply', body).status_code, 409)
        self.assertFalse(any(c[0] == 'patch' and '--dry-run=server' not in c for c in self.calls))

    def test_invalid_requests_never_reach_cluster(self):
        self.assertEqual(self.client.post('/api/log-control/apply', json=self.body).status_code, 403)
        self.assertEqual(self.post('apply').status_code, 400)
        for extra in ({'configmap': 'unrelated'}, {'mode': 'BAD'}, {'username': 'a\nb'},
                      {'configmap': 'fip-env', 'mode': 'TRACE'}, {'username': ''}, {'mode': []}):
            self.assertEqual(self.post('preview', {**self.body, **extra}).status_code, 400)
        self.oc_mock.assert_not_called()

    def test_immutable_or_unreferenced_config_blocks_changes(self):
        self.config['immutable'] = True
        self.assertEqual(self.post('preview').status_code, 400)
        self.config['immutable'] = False
        self.deployment = None
        self.assertEqual(self.post('preview').status_code, 400)
        self.assertFalse(any(c[0] == 'patch' for c in self.calls))

    def test_config_failure_never_restarts(self):
        body = self.confirmed()
        self.fail = 'configmap'
        self.assertEqual(self.post('apply', body).status_code, 502)
        self.assertFalse(any(c[:2] == ('patch', 'deployment') and '--dry-run=server' not in c for c in self.calls))

    def test_restart_failure_reports_partial_success(self):
        body = self.confirmed()
        self.fail = 'deployment'
        response = self.post('apply', body)
        self.assertEqual(response.status_code, 502)
        self.assertTrue(response.json['configmap_updated'])
        self.assertEqual(response.json['pending'], ['actual-ac'])
        self.assertEqual(response.json['restarted'], [])
        self.assertEqual(response.json['tracking']['names'], ['actual-ac'])

    def test_dry_run_failure_blocks_all_actual_changes(self):
        for kind in ('configmap', 'deployment'):
            body = self.confirmed()
            self.dry_fail = kind
            self.assertEqual(self.post('apply', body).status_code, 502)
            self.assertFalse(any(c[0] == 'patch' and '--dry-run=server' not in c for c in self.calls))
            self.dry_fail = None

    def test_every_dry_run_finishes_before_first_actual_write(self):
        body = self.confirmed()
        self.calls.clear()
        response = self.post('apply', body)
        self.assertEqual(response.status_code, 200)
        patches = [c for c in self.calls if c[0] == 'patch']
        self.assertEqual([('--dry-run=server' in c) for c in patches], [True, True, False, False])
        self.assertEqual(patches[0][:-1], patches[2])
        self.assertEqual(patches[1][:-1], patches[3])
        self.assertEqual(response.json['tracking']['names'], ['actual-ac'])

    def test_scaled_down_and_paused_deployments_block_preview(self):
        for field, value in [('replicas', 0), ('paused', True)]:
            self.deployment['spec'][field] = value
            self.assertEqual(self.post('preview').status_code, 400)
            del self.deployment['spec'][field]
        self.assertFalse(any(c[0] == 'patch' for c in self.calls))

    def test_rollout_waits_for_new_generation_and_all_replicas(self):
        d = self.deployment
        d['metadata']['generation'] = 8
        d['spec']['replicas'] = 2
        d['spec']['template']['metadata'] = {'annotations': {'kubectl.kubernetes.io/restartedAt': 'test-restart'}}
        d['status'] = {'observedGeneration': 7, 'updatedReplicas': 2, 'readyReplicas': 2,
                       'availableReplicas': 2, 'replicas': 2}
        self.assertEqual(module.rollout_state(d, 'test-restart')['state'], 'progressing')
        d['status']['observedGeneration'] = 8
        self.assertEqual(module.rollout_state(d, 'test-restart')['state'], 'ready')
        for key, value in [('updatedReplicas', 1), ('readyReplicas', 1), ('availableReplicas', 1),
                           ('replicas', 3), ('terminatingReplicas', 1)]:
            changed = copy.deepcopy(d)
            changed['status'][key] = value
            self.assertEqual(module.rollout_state(changed, 'test-restart')['state'], 'progressing')
        self.assertEqual(module.rollout_state(d, 'different-restart')['state'], 'changed')
        d['status']['conditions'] = [{'type': 'Progressing', 'status': 'False', 'reason': 'ProgressDeadlineExceeded'}]
        self.assertEqual(module.rollout_state(d, 'test-restart')['state'], 'failed')
        d['spec']['replicas'] = 0
        self.assertEqual(module.rollout_state(d, 'test-restart')['state'], 'blocked')

    def test_rollout_endpoint_only_reads_and_reports_missing_deployment(self):
        response = self.client.get('/api/log-control/rollout', query_string={
            'configmap': 'fnhttp-ac-env', 'restarted_at': 'test', 'names': 'missing'})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json['complete'])
        self.assertEqual(response.json['deployments'][0]['state'], 'changed')
        self.assertEqual(self.calls, [('get', 'deployments', '-o', 'json')])
        self.assertEqual(self.client.get('/api/log-control/rollout').status_code, 400)

    def test_reference_discovery(self):
        deployment = copy.deepcopy(self.deployment)
        spec = deployment['spec']['template']['spec']
        spec['initContainers'] = [{'env': [{'valueFrom': {'configMapKeyRef': {'name': 'init-env'}}}]}]
        spec['volumes'] = [{'configMap': {'name': 'mounted-env'}},
                           {'projected': {'sources': [{'configMap': {'name': 'projected-env'}}]}}]
        self.assertEqual(module.config_references(deployment), {'fnhttp-ac-env', 'init-env', 'mounted-env', 'projected-env'})

    def test_list_filters_configmaps_and_data(self):
        configs = [self.config, {'metadata': {'name': 'finbranch-fip-py-env'}},
                   {'metadata': {'name': 'fip-env'}}, {'metadata': {'name': 'unrelated-env'}}]
        self.oc_mock.side_effect = None
        self.oc_mock.return_value = json.dumps({'items': configs})
        response = self.client.get('/api/log-control')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json['configmaps']), 3)
        self.assertNotIn('UNRELATED', str(response.json))
        self.oc_mock.assert_called_once_with('get', 'configmaps', '-o', 'json')


if __name__ == '__main__':
    unittest.main()
