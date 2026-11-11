import copy
import json
import unittest
from unittest.mock import patch

import app as module
from fixtures import authenticated_client
from fixtures import healthy_deployment


class ResetDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.client = authenticated_client(module.app)
        self.headers = {'X-Finacle-Namespace': 'uat-finacle', 'X-Requested-With': 'FinacleSupport'}
        names = sorted(module.FNHTTP_CONFIGS) + ['fip-env'] + [f'finbranch-fip-{x}-env' for x in ('ac', 'la', 'py', 'rof')]
        self.configs = [{'metadata': {'name': n, 'resourceVersion': str(i)},
                         'data': {'CUSTOM': 'keep', 'TBA_DEBUG_LEVEL': '7', 'DEBUG_LOG_LEVEL': 'DEBUG', 'FI_LOGLEVEL': 'DEBUG'}}
                        for i, n in enumerate(names)]
        self.deployments, self.pods, self.rs = [], [], []
        for i, name in enumerate(names):
            d, p, rs = healthy_deployment('app-' + str(i), name)
            self.deployments.append(d); self.pods.append(p); self.rs.append(rs)
        self.calls = []
        self.fail_config = None
        self.make_unready_after_dry_run = False
        patch.object(module, 'oc', side_effect=self.oc).start()
        self.addCleanup(patch.stopall)

    def oc(self, *args):
        self.calls.append(args)
        if args[0] == 'get':
            if args[1] == 'configmap':
                return json.dumps(next(c for c in self.configs if c['metadata']['name'] == args[2]))
            return json.dumps({'items': {'configmaps': self.configs, 'deployments': self.deployments,
                                        'pods': self.pods, 'replicasets': self.rs}[args[1]]})
        if args[0] == 'patch':
            if '--dry-run=server' in args:
                if self.make_unready_after_dry_run:
                    self.pods[0]['status']['conditions'][0]['status'] = 'False'
            elif args[1] == 'configmap' and args[2] == self.fail_config:
                raise module.ClusterError('conflict')
            return '{}'
        raise AssertionError(args)

    def post(self, action, body=None):
        return self.client.post('/api/log-control/' + action, json=body or {}, headers=self.headers)

    def preview(self):
        response = self.post('reset-preview')
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def test_all_nine_defaults_only_touch_expected_keys_and_project(self):
        self.configs.append({'metadata': {'name': 'unrelated-env', 'resourceVersion': '99'}, 'data': {'SECRET': 'never-return'}})
        plan = self.preview()
        self.assertEqual(plan['namespace'], 'uat-finacle')
        self.assertEqual(len(plan['configurations']), 9)
        self.assertFalse(any(c[0] == 'patch' and '--dry-run=server' not in c for c in self.calls))
        response = self.post('reset-apply', {'confirmed': True, 'fingerprint': plan['fingerprint']})
        self.assertEqual(response.status_code, 200, response.json)
        writes = [c for c in self.calls if c[0] == 'patch' and '--dry-run=server' not in c]
        self.assertEqual(len(writes), 18)
        for command in writes[:9]:
            name = command[2]
            data = json.loads(command[-1])[1]['value']
            self.assertEqual(data['CUSTOM'], 'keep')
            self.assertEqual(data['TBA_DEBUG_LEVEL'], '7')
            for key, value in module.logging_values(module.log_group(name), 'NORMAL', 'ALL').items():
                self.assertEqual(data[key], value)
        self.assertEqual(response.json['tracking']['namespace'], 'uat-finacle')
        self.assertNotIn('never-return', str(plan))

    def test_shared_deployment_restarted_once(self):
        self.configs = self.configs[:2]
        self.deployments = self.deployments[:1]; self.pods = self.pods[:1]; self.rs = self.rs[:1]
        self.deployments[0]['spec']['template']['spec']['containers'][0]['envFrom'].append(
            {'configMapRef': {'name': self.configs[1]['metadata']['name']}})
        plan = self.preview()
        self.assertEqual(len(plan['deployments']), 1)
        response = self.post('reset-apply', {'confirmed': True, 'fingerprint': plan['fingerprint']})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json['restarted']), 1)

    def test_cross_project_preview_cannot_be_applied(self):
        plan = self.preview()
        self.headers['X-Finacle-Namespace'] = 'pre-uat-finacle'
        self.assertEqual(self.post('reset-apply', {'confirmed': True, 'fingerprint': plan['fingerprint']}).status_code, 409)
        self.assertFalse(any(c[0] == 'patch' and '--dry-run=server' not in c for c in self.calls))

    def test_readiness_rechecked_after_dry_run(self):
        plan = self.preview()
        self.make_unready_after_dry_run = True
        self.assertEqual(self.post('reset-apply', {'confirmed': True, 'fingerprint': plan['fingerprint']}).status_code, 409)
        self.assertFalse(any(c[0] == 'patch' and '--dry-run=server' not in c for c in self.calls))

    def test_partial_config_failure_reports_progress_and_does_not_restart(self):
        plan = self.preview()
        self.fail_config = plan['configurations'][1]['configmap']
        response = self.post('reset-apply', {'confirmed': True, 'fingerprint': plan['fingerprint']})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(len(response.json['updated']), 1)
        self.assertEqual(response.json['restarted'], [])
        self.assertFalse(any(c[:2] == ('patch', 'deployment') and '--dry-run=server' not in c for c in self.calls))

    def test_current_values_only_return_logging_keys_and_never_write(self):
        response = self.client.get('/api/log-control/current?configmap=fnhttp-ac-env', headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['values']['DEBUG_LOG_LEVEL'], 'DEBUG')
        self.assertNotIn('CUSTOM', response.json['values'])
        self.assertEqual(self.calls, [('get', 'configmap', 'fnhttp-ac-env', '-o', 'json')])


if __name__ == '__main__':
    unittest.main()
