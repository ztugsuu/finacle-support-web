import json
import subprocess
import unittest
from unittest.mock import patch, Mock

import app as module
from fixtures import healthy_deployment


def pod(name='fip-7b7c-x1', phase='Running', ready=True):
    return {'metadata': {'name': name}, 'spec': {'containers': [{'name': 'finacle'}, {'name': 'sidecar'}]},
            'status': {'phase': phase, 'containerStatuses': [
                {'name': 'finacle', 'ready': ready, 'restartCount': 2},
                {'name': 'sidecar', 'ready': ready, 'restartCount': 0}]}}


class AppTests(unittest.TestCase):
    def setUp(self):
        self.client = module.app.test_client()
        self.client.environ_base['HTTP_X_FINACLE_NAMESPACE'] = 'pre-uat-finacle'

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

    def test_fnhttp_deployment_files_and_missing_directory(self):
        deployment, item, rs = healthy_deployment('fnhttpac')
        item['spec']['containers'] = [{'name': 'sidecar'}, {'name': 'fnhttpac'}]
        job = pod('fnhttppy-restart-29905680-7nrxq')
        job['metadata']['ownerReferences'] = [{'kind': 'Job', 'uid': 'job', 'controller': True}]
        def resources(kind):
            return {'pods': [item, job], 'deployments': [deployment], 'replicasets': [rs]}[kind]
        with patch.object(module, 'resources', side_effect=resources):
            result = module.pods()
            self.assertEqual(result[0]['source'], 'fnhttp')
            self.assertEqual(result[0]['fnhttp_service'], 'ac')
            self.assertIsNone(result[1]['source'])
            for output, expected in [('', []),
                    ('100' + '\0' + '200' + '\0' + '1500000' + '\0' + module.SOURCES['fnhttp']['path'] + '/debug.log\0' + '100' + '\0' + '200' + '\0' + '1500000' + '\0/etc/secret.log\0', ['debug.log'])]:
                with patch.object(module, 'oc', return_value=output):
                    response = self.client.get('/api/log-files?source=fnhttp')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json['files'], expected)
            with patch.object(module, 'oc', return_value='last line') as execute:
                response = self.client.get('/api/logs?source=fnhttp&file=debug.log')
                self.assertEqual(response.json['content'], 'last line')
                self.assertEqual(execute.call_args.args[3], 'fnhttpac')
                self.assertEqual(execute.call_args.args[-1], module.SOURCES['fnhttp']['path'] + '/debug.log')
            with patch.object(module, 'oc', side_effect=module.ClusterError('No such file or directory')):
                self.assertEqual(self.client.get('/api/logs?source=fnhttp&file=debug.log').status_code, 404)
            with patch.object(module, 'oc') as execute:
                self.assertEqual(self.client.get('/api/logs?source=fnhttp&file=../../secret.log').status_code, 400)
                self.assertEqual(self.client.get('/api/log-files?source=fnhttp&pod=' + job['metadata']['name']).status_code, 404)
                self.assertEqual(self.client.get('/api/log-files?source=fnhttp&container=sidecar').status_code, 400)
                self.assertEqual(self.client.get('/api/log-files?source=fnhttp&service=py').status_code, 404)
                execute.assert_not_called()

    def test_fnhttp_lists_and_reads_all_file_extensions(self):
        deployment, item, rs = healthy_deployment('fnhttpac')
        item['spec']['containers'] = [{'name': 'fnhttpac'}]
        names = ['chkIfChnlEnbForInqAudit.scr_4383_1110.stdout',
                 'chkIfChnlEnbForInqAudit.scr_4383_1110.trc',
                 'no_extension', '.hidden', 'with spaces.stdout', 'debug.log']
        directory = module.SOURCES['fnhttp']['path']
        def resources(kind):
            return {'pods': [item], 'deployments': [deployment], 'replicasets': [rs]}[kind]
        with patch.object(module, 'resources', side_effect=resources):
            listing = ''.join('100' + '\0' + '200' + '\0' + '1500000' + '\0' + directory + '/' + name + '\0' for name in names)
            with patch.object(module, 'oc', return_value=listing) as execute:
                response = self.client.get('/api/log-files?source=fnhttp')
                self.assertEqual(response.json['files'], sorted(names))
                self.assertNotIn('-name', execute.call_args.args[-3])
            for name in names:
                with self.subTest(filename=name), patch.object(module, 'oc', return_value='trace content') as execute:
                    response = self.client.get('/api/logs', query_string={'source': 'fnhttp', 'file': name})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json['content'], 'trace content')
                    self.assertEqual(execute.call_args.args[-1], directory + '/' + name)
            for name in ['.', '..', '../secret', 'dir/file', 'dir\\file', 'bad\nname']:
                with patch.object(module, 'oc') as execute:
                    self.assertEqual(self.client.get('/api/logs', query_string={'source': 'fnhttp', 'file': name}).status_code, 400)
                    execute.assert_not_called()

    def test_fnhttp_files_sort_by_creation_time_with_modified_fallback(self):
        directory = module.SOURCES['fnhttp']['path']
        records = [('100', '900', 'old.stdout'), ('300', '400', 'new.trc'),
                   ('0', '200', 'fallback'), ('-1', '50', 'unknown-birth')]
        output = ''.join('\0'.join([birth, modified, '1500000', directory + '/' + name]) + '\0'
                         for birth, modified, name in records)
        selected = {'name': 'fnhttpac-pod', 'source': 'fnhttp', 'phase': 'Running',
                    'fnhttp_service': 'ac', 'containers': ['fnhttpac']}
        with patch.object(module, 'selected_log_pod', return_value=selected), patch.object(module, 'oc', return_value=output):
            response = self.client.get('/api/log-files?source=fnhttp')
        self.assertEqual(response.json['files'], ['new.trc', 'fallback', 'old.stdout', 'unknown-birth'])
        details = response.json['file_details']
        self.assertEqual(details[0]['time_kind'], 'created')
        self.assertEqual(details[0]['size_bytes'], 1500000)
        self.assertEqual(details[1]['time_kind'], 'modified')
        self.assertEqual(details[1]['timestamp'], 200)
        self.assertEqual(details[0]['time'], '1970-01-01T00:05:00+00:00')

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
        deployment, pod_item, rs = healthy_deployment('fip', 'fip-env')
        with patch.object(module, 'resources', side_effect=lambda kind: {'deployments': [deployment], 'pods': [pod_item], 'replicasets': [rs]}[kind]), patch.object(module, 'oc') as execute:
            self.assertEqual(self.client.post('/restart', headers=headers, json={'name': 'other'}).status_code, 404)
            execute.assert_not_called()
            self.assertEqual(self.client.post('/restart', headers=headers, json={'name': 'fip'}).status_code, 200)
            execute.assert_called_once()
            self.assertEqual(execute.call_args.args[:4], ('patch', 'deployment', 'fip', '--type=merge'))
            self.assertEqual(json.loads(execute.call_args.args[-1])['metadata']['resourceVersion'], '31')

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

