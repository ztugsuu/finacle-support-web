import os
import unittest
from unittest.mock import patch, Mock

from flask import Flask
import app as module
from authentication import init_auth, authenticate, LDAPUnavailable, service_bind_error


class AuthenticationTests(unittest.TestCase):
    @patch.dict(os.environ, {'LDAP_URL': 'ldap://my-ip:389', 'LDAP_BASE_DN': 'dc=mbank,dc=local',
                            'LDAP_BIND_DN': 'my-dn', 'LDAP_BIND_PASSWORD': 'service-secret',
                            'LDAP_USERNAME_ATTRIBUTE': 'sAMAccountName', 'LDAP_INSECURE': 'true'})
    def test_search_then_bind_with_user_password(self):
        with patch('authentication.Connection') as factory, patch('authentication.Server'), patch('authentication.Tls'):
            connection = factory.return_value
            connection.closed = False
            connection.bind.return_value = True
            connection.search.return_value = True
            connection.result = {'result': 0}
            connection.entries = [Mock(entry_dn='cn=Operator,dc=mbank,dc=local')]
            connection.rebind.return_value = True
            self.assertTrue(authenticate('operator', 'user-secret'))
            self.assertEqual(factory.call_args.kwargs['password'], 'service-secret')
            connection.start_tls.assert_not_called()
            connection.search.assert_called_once_with('dc=mbank,dc=local', '(sAMAccountName=operator)',
                                                      attributes=['1.1'], size_limit=2, time_limit=5)
            connection.rebind.assert_called_once_with(user='cn=Operator,dc=mbank,dc=local', password='user-secret')
            connection.rebind.reset_mock()
            connection.entries = []
            self.assertFalse(authenticate('missing', 'user-secret'))
            connection.rebind.assert_not_called()
            connection.entries = [Mock(), Mock()]
            self.assertFalse(authenticate('duplicate', 'user-secret'))
            connection.rebind.assert_not_called()

    @patch.dict(os.environ, {'LDAP_URL': 'ldap://172.18.31.10:389', 'LDAP_BASE_DN': 'dc=mbank,dc=local',
                            'LDAP_BIND_DN': 'openshift@mbank.local', 'LDAP_BIND_PASSWORD': 'service-test-only',
                            'LDAP_USERNAME_ATTRIBUTE': 'sAMAccountName', 'LDAP_INSECURE': 'true'})
    def test_admin_service_bind_failure_does_not_attempt_user_search(self):
        with patch('authentication.Connection') as factory, patch('authentication.Server'), patch('authentication.Tls'):
            connection = factory.return_value
            connection.closed = False
            connection.bind.return_value = False
            with self.assertRaises(LDAPUnavailable):
                authenticate('operator', 'user-test-only')
            self.assertEqual(factory.call_args.kwargs['user'], 'openshift@mbank.local')
            connection.search.assert_not_called()
            connection.rebind.assert_not_called()
            connection.unbind.assert_called_once()

    def test_service_bind_diagnostic_does_not_expose_directory_message(self):
        message = service_bind_error({'result': 49, 'message': 'sensitive-directory-detail'})
        self.assertIn('49', message)
        self.assertNotIn('sensitive-directory-detail', message)
        self.assertIn('TLS', service_bind_error({'result': 8}))
        self.assertNotIn('sensitive-directory-detail', service_bind_error({'result': 1, 'message': 'sensitive-directory-detail'}))

    def test_real_app_requires_login_for_both_projects_and_zip_download(self):
        client = module.app.test_client()
        with patch.object(module, 'oc_bytes') as execute:
            for namespace in module.NAMESPACES:
                headers = {'X-Finacle-Namespace': namespace, 'X-Requested-With': 'FinacleSupport'}
                self.assertEqual(client.get('/api/overview', headers=headers).status_code, 401)
                self.assertEqual(client.post('/api/logs/download?source=fnhttp', headers=headers,
                                            json={'files': ['test.trc']}).status_code, 401)
            execute.assert_not_called()
        self.assertEqual(client.get('/health').status_code, 200)

    def setUp(self):
        self.app = Flask(__name__, template_folder=module.app.template_folder,
                         root_path=module.app.root_path)
        init_auth(self.app)
        self.app.config.update(TESTING=True, SECRET_KEY='auth-test-secret', SESSION_COOKIE_SECURE=False)
        self.client = self.app.test_client()
        self.app.add_url_rule('/', 'index', lambda: 'private')
        self.app.add_url_rule('/health', 'health', lambda: 'UP')

    def token(self):
        self.client.get('/login')
        with self.client.session_transaction() as state:
            return state['csrf_token']

    def test_protected_routes_and_public_health(self):
        self.assertEqual(self.client.get('/').location, '/login')
        for path in ('/api/overview', '/api/logs', '/api/log-control'):
            self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.post('/restart').status_code, 401)
        self.assertEqual(self.client.get('/health').status_code, 200)

    def test_login_logout_and_no_password_in_session(self):
        token = self.token()
        with patch('authentication.authenticate', return_value=True) as bind:
            response = self.client.post('/login', data={'username': 'operator', 'password': 'secret', 'csrf_token': token})
        bind.assert_called_once_with('operator', 'secret')
        self.assertEqual(response.location, '/')
        self.assertEqual(self.client.get('/').status_code, 200)
        with self.client.session_transaction() as state:
            self.assertNotIn('password', state)
            self.assertNotEqual(state['csrf_token'], token)
            logout_token = state['csrf_token']
        self.assertEqual(self.client.post('/logout').status_code, 403)
        self.assertEqual(self.client.post('/logout', data={'csrf_token': logout_token}).status_code, 302)
        self.assertEqual(self.client.get('/').status_code, 302)

    def test_failed_login_csrf_and_missing_config(self):
        token = self.token()
        with patch('authentication.authenticate', return_value=False) as bind:
            self.assertEqual(self.client.post('/login', data={'username': 'x', 'password': 'bad'}).status_code, 400)
            bind.assert_not_called()
            self.assertEqual(self.client.post('/login', data={'username': 'x', 'password': 'bad', 'csrf_token': token}).status_code, 401)
        with patch('authentication.authenticate', side_effect=LDAPUnavailable('Unavailable')):
            self.assertEqual(self.client.post('/login', data={'csrf_token': token}).status_code, 503)
        self.app.secret_key = None
        self.assertEqual(self.client.get('/login').status_code, 503)

    @patch.dict(os.environ, {'LDAP_URL': 'ldap://dc.example.com:389', 'LDAP_USER_DN_TEMPLATE': '{username}@example.com'})
    def test_starttls_before_bind_and_reject_blank_password(self):
        with patch('authentication.Connection') as factory, patch('authentication.Server'), patch('authentication.Tls'):
            connection = factory.return_value
            connection.closed = False
            connection.start_tls.return_value = False
            with self.assertRaises(LDAPUnavailable):
                authenticate('operator', 'secret')
            connection.bind.assert_not_called()
            connection.unbind.assert_called_once()
            factory.reset_mock()
            self.assertFalse(authenticate('operator', ''))
            factory.assert_not_called()

    @patch.dict(os.environ, {'LDAP_URL': 'ldaps://dc.example.com:636', 'LDAP_USER_DN_TEMPLATE': '{username}@example.com'})
    def test_ldaps_credentials_and_tls_validation(self):
        with patch('authentication.Connection') as factory, patch('authentication.Server') as server, patch('authentication.Tls') as tls:
            connection = factory.return_value
            connection.closed = False
            connection.bind.return_value = True
            self.assertTrue(authenticate('operator', 'secret'))
            self.assertTrue(server.call_args.kwargs['use_ssl'])
            import ssl
            self.assertEqual(tls.call_args.kwargs['validate'], ssl.CERT_REQUIRED)
            connection.start_tls.assert_not_called()
            connection.bind.return_value = False
            connection.result = {'result': 49}
            self.assertFalse(authenticate('operator', 'wrong'))
