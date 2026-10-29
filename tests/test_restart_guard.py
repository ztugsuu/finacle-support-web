import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, Mock
from concurrent.futures import ThreadPoolExecutor

import app as module
from restart_guard import eligibility
from fixtures import healthy_deployment


class RestartGuardTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 28, 6, 0, tzinfo=timezone.utc)
        self.d, self.p, self.rs = healthy_deployment(now=self.now)

    def check(self):
        return eligibility(self.d, [self.p], [self.rs], self.now)

    def test_stable_ready_deployment_is_allowed(self):
        self.assertTrue(self.check()['allowed'])
        # Historical restarts do not block forever after recovery.
        self.p['status']['containerStatuses'][0]['restartCount'] = 7
        self.assertTrue(self.check()['allowed'])

    def test_every_instability_blocks_restart(self):
        original = copy.deepcopy((self.d, self.p, self.rs))
        for change in ('rollout', 'missing-container', 'unready', 'crash', 'terminating', 'failed-pod',
                       'unknown-owner', 'missing-time', 'paused', 'zero', 'old-replicas', 'replica-error'):
            with self.subTest(change=change):
                self.d, self.p, self.rs = copy.deepcopy(original)
                if change == 'rollout': self.d['status']['observedGeneration'] = 0
                if change == 'missing-container': self.p['status']['containerStatuses'] = []
                if change == 'unready': self.p['status']['conditions'][0]['status'] = 'False'
                if change == 'crash': self.p['status']['containerStatuses'][0]['state'] = {'waiting': {'reason': 'CrashLoopBackOff'}}
                if change == 'terminating': self.p['metadata']['deletionTimestamp'] = self.now.isoformat()
                if change == 'failed-pod': self.p['status']['phase'] = 'Failed'
                if change == 'unknown-owner': self.rs['metadata']['ownerReferences'][0]['uid'] = 'other'
                if change == 'missing-time': del self.p['status']['conditions'][0]['lastTransitionTime']
                if change == 'paused': self.d['spec']['paused'] = True
                if change == 'zero': self.d['spec']['replicas'] = 0
                if change == 'old-replicas': self.d['status']['replicas'] = 2
                if change == 'replica-error': self.d['status']['conditions'].append({'type': 'ReplicaFailure', 'status': 'True'})
                self.assertFalse(self.check()['allowed'])

    def test_five_minutes_from_latest_recovery(self):
        recent = (self.now - timedelta(seconds=299)).isoformat()
        self.p['status']['conditions'][0]['lastTransitionTime'] = recent
        self.assertEqual(self.check()['cooldown_seconds'], 1)
        self.assertTrue(eligibility(self.d, [self.p], [self.rs], self.now + timedelta(seconds=1))['allowed'])
        self.p['status']['containerStatuses'][0]['state']['running']['startedAt'] = self.now.isoformat()
        self.assertEqual(self.check()['cooldown_seconds'], 300)

    def test_restart_annotation_blocks_even_if_status_is_stale(self):
        self.d['spec']['template']['metadata'] = {'annotations': {'kubectl.kubernetes.io/restartedAt': self.now.isoformat()}}
        self.assertEqual(self.check()['cooldown_seconds'], 300)

    def test_other_deployments_do_not_affect_eligibility(self):
        other_d, other_p, other_rs = healthy_deployment('other', now=self.now)
        other_p['status']['phase'] = 'Failed'
        self.assertTrue(eligibility(self.d, [self.p, other_p], [self.rs, other_rs], self.now)['allowed'])

    def test_init_containers_and_running_sidecars(self):
        old = (self.now - timedelta(minutes=10)).isoformat()
        self.p['spec']['initContainers'] = [{'name': 'setup'}, {'name': 'proxy', 'restartPolicy': 'Always'}]
        self.p['status']['initContainerStatuses'] = [
            {'name': 'setup', 'state': {'terminated': {'exitCode': 0, 'finishedAt': old}}},
            {'name': 'proxy', 'ready': True, 'state': {'running': {'startedAt': old}}}]
        self.assertTrue(self.check()['allowed'])
        self.p['status']['initContainerStatuses'][1]['ready'] = False
        self.assertFalse(self.check()['allowed'])


class NamespaceTests(unittest.TestCase):
    def test_bad_and_conflicting_namespaces_never_reach_cluster(self):
        client = module.app.test_client()
        with patch.object(module, 'oc') as oc:
            self.assertEqual(client.get('/api/overview?namespace=production').status_code, 400)
            self.assertEqual(client.get('/api/overview?namespace=uat-finacle', headers={'X-Finacle-Namespace': 'pre-uat-finacle'}).status_code, 400)
            self.assertEqual(client.post('/restart', json={'name': 'fip'}, headers={'X-Requested-With': 'FinacleSupport'}).status_code, 400)
            oc.assert_not_called()

    def test_each_request_uses_its_own_namespace_including_parallel_requests(self):
        seen = []
        def run(command, **kwargs):
            seen.append(command)
            return Mock(returncode=0, stdout=b'{"items":[]}', stderr=b'')
        def request(namespace):
            client = module.app.test_client()
            return client.get('/api/log-control', headers={'X-Finacle-Namespace': namespace}).status_code
        with patch.object(module.subprocess, 'run', side_effect=run), ThreadPoolExecutor(2) as pool:
            self.assertEqual(list(pool.map(request, module.NAMESPACES)), [200, 200])
        self.assertEqual({cmd[2] for cmd in seen}, set(module.NAMESPACES))
        self.assertTrue(all(cmd[:2] == ['oc', '-n'] for cmd in seen))

    def test_uat_page_shows_uat_without_preuat_banner(self):
        page = module.app.test_client().get('/?namespace=uat-finacle').data.decode()
        self.assertIn('value="uat-finacle" selected', page)
        self.assertNotIn('PRE-UAT ENVIRONMENT', page)

    def test_server_refuses_direct_restart_when_pod_not_ready(self):
        deployment, pod, rs = healthy_deployment('fip', 'fip-env')
        pod['status']['containerStatuses'][0]['ready'] = False
        with patch.object(module, 'resources', side_effect=lambda k: {'deployments': [deployment], 'pods': [pod], 'replicasets': [rs]}[k]), patch.object(module, 'oc') as oc:
            response = module.app.test_client().post('/restart', json={'name': 'fip'}, headers={
                'X-Requested-With': 'FinacleSupport', 'X-Finacle-Namespace': 'uat-finacle'})
            self.assertEqual(response.status_code, 409)
            oc.assert_not_called()

    def test_second_restart_is_rejected_even_before_pod_status_catches_up(self):
        deployment, pod, rs = healthy_deployment('fip', 'fip-env')
        def write(*args):
            payload = json.loads(args[-1])
            deployment['spec']['template']['metadata'] = payload['spec']['template']['metadata']
            deployment['metadata']['resourceVersion'] = '32'
            return '{}'
        headers = {'X-Requested-With': 'FinacleSupport', 'X-Finacle-Namespace': 'pre-uat-finacle'}
        client = module.app.test_client()
        with patch.object(module, 'resources', side_effect=lambda k: {'deployments': [deployment], 'pods': [pod], 'replicasets': [rs]}[k]), patch.object(module, 'oc', side_effect=write) as oc:
            self.assertEqual(client.post('/restart', json={'name': 'fip'}, headers=headers).status_code, 200)
            self.assertEqual(client.post('/restart', json={'name': 'fip'}, headers=headers).status_code, 409)
            self.assertEqual(oc.call_count, 1)

    def test_missing_replicaset_permission_disables_buttons_but_preserves_pods(self):
        d, p, _ = healthy_deployment()
        def resources(kind):
            if kind == 'replicasets': raise module.ClusterError('forbidden')
            return {'deployments': [d], 'pods': [p]}[kind]
        with patch.object(module, 'resources', side_effect=resources):
            response = module.app.test_client().get('/api/overview')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json['pods']), 1)
        self.assertFalse(response.json['deployments'][0]['restart']['allowed'])


if __name__ == '__main__':
    unittest.main()
