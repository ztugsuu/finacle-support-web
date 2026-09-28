import json
import subprocess
import unittest
from unittest.mock import patch, Mock

import app as module


def pod(name='fip-7b7c-x1', phase='Running', ready=True):
    return {'metadata': {'name': name}, 'spec': {'containers': [{'name': 'finacle'}, {'name': 'sidecar'}]},
            'status': {'phase': phase, 'containerStatuses': [
                {'name': 'finacle', 'ready': ready, 'restartCount': 2},
                {'name': 'sidecar', 'ready': ready, 'restartCount': 0}]}}


class AppTests(unittest.TestCase):
    def setUp(self):
        self.client = module.app.test_client()

    def test_page_load_does_not_require_cluster(self):
        with patch.object(module, 'oc', side_effect=AssertionError('unexpected cluster call')):
            response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'pre-uat-finacle', response.data)
        self.assertEqual(self.client.get('/health').json['status'], 'UP')

    def test_all_five_files_use_fixed_namespace_path_and_container(self):
        for source, config in module.SOURCES.items():
            for filename in config['files']:
                with self.subTest(source=source, filename=filename):
                    def run(command, **kwargs):
                        self.assertEqual(command[:4], ['oc', '-n', 'pre-uat-finacle', '--request-timeout=15s'])
                        self.assertNotIn('shell', kwargs)
                        output = json.dumps({'items': [pod(source + '-abc-1')]}) if command[4] == 'get' else 'one\ntwo\nthree\n'
                        return Mock(returncode=0, stdout=output.encode(), stderr=b'')
                    with patch.object(module.subprocess, 'run', side_effect=run) as execute:
                        response = self.client.get('/api/logs', query_string={
                            'source': source, 'file': filename, 'container': 'sidecar', 'lines': 2})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json['content'], 'two\nthree')
                    self.assertEqual(execute.call_args.args[0][4:], ['exec', source + '-abc-1', '-c', 'sidecar', '--', 'tail', '-c', '262144', '--', config['path'] + '/' + filename])

    def test_rejects_arbitrary_paths_and_invalid_lines_before_cluster(self):
        for query in [{'source': 'fip', 'file': '../../etc/passwd'},
                      {'source': 'other', 'file': 'ciErrorLog.log'},
                      {'source': 'fip', 'file': 'ciErrorLog.log', 'lines': '99999'}]:
            with patch.object(module, 'oc') as execute:
                self.assertEqual(self.client.get('/api/logs', query_string=query).status_code, 400)
                execute.assert_not_called()

    def test_pod_and_container_allowlist(self):
        base = {'source': 'fip', 'file': 'ciErrorLog.log'}
        for extra, status in [({'pod': 'fnbrnmpy-1'}, 404), ({'container': '--bad'}, 400)]:
            with patch.object(module, 'resources', return_value=[pod()]), patch.object(module, 'oc') as execute:
                self.assertEqual(self.client.get('/api/logs', query_string={**base, **extra}).status_code, status)
                execute.assert_not_called()
        self.assertIsNone(module.source_for('fipother-abc'))

    def test_prefers_healthy_pod_and_handles_nonrunning(self):
        with patch.object(module, 'resources', return_value=[pod('fip-old', ready=False), pod('fip-ready')]), patch.object(module, 'oc', return_value='ok'):
            response = self.client.get('/api/logs?source=fip&file=ciErrorLog.log')
            self.assertEqual(response.json['pod'], 'fip-ready')
        with patch.object(module, 'resources', return_value=[pod(phase='Pending')]):
            self.assertEqual(self.client.get('/api/logs?source=fip&file=ciErrorLog.log').status_code, 409)

    def test_cluster_failure_and_timeout_are_not_empty_success(self):
        for error in [subprocess.TimeoutExpired('oc', 20), FileNotFoundError()]:
            with patch.object(module.subprocess, 'run', side_effect=error):
                self.assertEqual(self.client.get('/api/overview').status_code, 502)
        with patch.object(module.subprocess, 'run', return_value=Mock(returncode=1, stderr=b'forbidden')):
            self.assertEqual(self.client.get('/api/overview').json['message'], 'forbidden')

    def test_deployment_failure_keeps_pod_overview(self):
        with patch.object(module, 'resources', side_effect=[[pod()], module.ClusterError('forbidden')]):
            response = self.client.get('/api/overview')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json['warning'], 'forbidden')
            self.assertEqual(response.json['pods'][0]['restarts'], 2)

    def test_restart_requires_custom_header_json_and_existing_deployment(self):
        self.assertEqual(self.client.post('/restart', json={'name': 'fip'}).status_code, 403)
        headers = {'X-Requested-With': 'FinacleSupport'}
        with patch.object(module, 'resources', return_value=[{'metadata': {'name': 'fip'}}]), patch.object(module, 'oc') as execute:
            self.assertEqual(self.client.post('/restart', headers=headers, json={'name': 'other'}).status_code, 404)
            execute.assert_not_called()
            self.assertEqual(self.client.post('/restart', headers=headers, json={'name': 'fip'}).status_code, 200)
            execute.assert_called_once_with('rollout', 'restart', 'deployment/fip')

    def test_html_logs_are_returned_as_text(self):
        with patch.object(module, 'resources', return_value=[pod()]), patch.object(module, 'oc', return_value='<script>alert(1)</script>'):
            response = self.client.get('/logs/fip-7b7c-x1')
            self.assertTrue(response.content_type.startswith('text/plain'))
            self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')

    def test_byte_limit_indicated_and_partial_first_line_removed(self):
        with patch.object(module, 'resources', return_value=[pod()]), patch.object(module, 'oc', return_value='x' * (module.MAX_BYTES - 4) + '\nend'):
            response = self.client.get('/api/logs?source=fip&file=ciErrorLog.log')
            self.assertTrue(response.json['truncated'])
            self.assertEqual(response.json['content'], 'end')


if __name__ == '__main__':
    unittest.main()

