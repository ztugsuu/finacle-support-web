"""LDAP direct or search-and-bind login; passwords never enter the session."""
import hmac
import os
import re
import secrets
import ssl
from datetime import timedelta
from urllib.parse import urlsplit

from flask import jsonify, redirect, render_template, request, session, url_for
from ldap3 import Connection, Server, Tls
from ldap3.core.exceptions import LDAPException
from ldap3.utils.dn import escape_rdn
from ldap3.utils.conv import escape_filter_chars


class LDAPUnavailable(Exception):
    pass


def authenticate(username, password):
    try:
        parsed = urlsplit(os.environ.get('LDAP_URL', ''))
    except ValueError as exc:
        raise LDAPUnavailable('LDAP URL тохиргоо буруу байна.') from exc
    template = os.environ.get('LDAP_USER_DN_TEMPLATE', '')
    base_dn = os.environ.get('LDAP_BASE_DN', '')
    bind_dn = os.environ.get('LDAP_BIND_DN', '')
    bind_password = os.environ.get('LDAP_BIND_PASSWORD', '')
    attribute = os.environ.get('LDAP_USERNAME_ATTRIBUTE', 'sAMAccountName')
    search_mode = bool(base_dn)
    insecure = os.environ.get('LDAP_INSECURE', 'false').lower() == 'true'
    if (parsed.scheme not in ('ldap', 'ldaps') or not parsed.hostname
            or parsed.username or parsed.password or parsed.path not in ('', '/')
            or parsed.query or parsed.fragment
            or (not search_mode and template.count('{username}') != 1)
            or (search_mode and (not bind_dn or not bind_password or not re.fullmatch(r'[A-Za-z][A-Za-z0-9-]*', attribute)))):
        raise LDAPUnavailable('LDAP тохиргоо дутуу эсвэл буруу байна.')
    if not re.fullmatch(r'[A-Za-z0-9_.@-]{1,128}', username) or not password or len(password) > 1024:
        return False
    connection = None
    try:
        tls = Tls(validate=ssl.CERT_NONE if insecure else ssl.CERT_REQUIRED,
                  ca_certs_file=os.environ.get('LDAP_CA_CERT_FILE') or None)
        server = Server(parsed.hostname, port=parsed.port or (636 if parsed.scheme == 'ldaps' else 389),
                        use_ssl=parsed.scheme == 'ldaps', tls=tls, connect_timeout=5)
        connection = Connection(server, user=bind_dn if search_mode else template.replace('{username}', escape_rdn(username)),
                                password=bind_password if search_mode else password,
                                receive_timeout=10, auto_referrals=False)
        connection.open(read_server_info=False)
        if connection.closed:
            raise LDAPUnavailable('LDAP сервертэй холбогдох боломжгүй байна.')
        if parsed.scheme == 'ldap' and not insecure and not connection.start_tls(read_server_info=False):
            raise LDAPUnavailable('LDAP TLS холболт амжилтгүй.')
        if search_mode:
            if not connection.bind():
                raise LDAPUnavailable('LDAP хайлтын эрхийн тохиргоог шалгана уу.')
            found = connection.search(base_dn, f'({attribute}={escape_filter_chars(username)})',
                                      attributes=['1.1'], size_limit=2, time_limit=5)
            if connection.result.get('result') != 0:
                raise LDAPUnavailable('LDAP хэрэглэгчийн хайлт амжилтгүй.')
            if not found or len(connection.entries) != 1:
                return False
            if connection.rebind(user=connection.entries[0].entry_dn, password=password):
                return True
            if connection.result.get('result') == 49:
                return False
            raise LDAPUnavailable('LDAP нэвтрэлт түр боломжгүй байна.')
        if connection.bind():
            return True
        if connection.result.get('result') == 49:
            return False
        raise LDAPUnavailable('LDAP нэвтрэлт түр боломжгүй байна.')
    except (LDAPException, OSError, ValueError) as exc:
        raise LDAPUnavailable('LDAP холболт эсвэл TLS тохиргоог шалгана уу.') from exc
    finally:
        if connection is not None:
            try:
                connection.unbind()
            except (LDAPException, OSError):
                pass


def init_auth(app):
    app.config.update(SECRET_KEY=os.environ.get('SECRET_KEY') or None,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                      SESSION_COOKIE_SECURE=os.environ.get('SESSION_COOKIE_SECURE', 'true').lower() != 'false',
                      PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
                      SESSION_REFRESH_EACH_REQUEST=False, MAX_CONTENT_LENGTH=16384)

    @app.before_request
    def require_login():
        if request.endpoint in ('login', 'health', 'static'):
            return
        if not session.get('username'):
            if request.path.startswith('/api/') or request.path == '/restart':
                return jsonify(message='Нэвтрэх шаардлагатай.', login_url=url_for('login')), 401
            return redirect(url_for('login'))

    @app.get('/login')
    @app.post('/login')
    def login():
        if not app.secret_key:
            return render_template('login.html', error='SECRET_KEY болон LDAP тохиргоог администратор оруулах шаардлагатай.'), 503
        if session.get('username'):
            return redirect(url_for('index'))
        session.setdefault('csrf_token', secrets.token_urlsafe(32))
        error, status = None, 200
        if request.method == 'POST':
            if not hmac.compare_digest(session['csrf_token'].encode(), request.form.get('csrf_token', '').encode()):
                error, status = 'Хүсэлтийн хугацаа дууссан. Дахин оролдоно уу.', 400
            else:
                username = request.form.get('username', '').strip()
                try:
                    if authenticate(username, request.form.get('password', '')):
                        session.clear()
                        session.update(username=username, csrf_token=secrets.token_urlsafe(32))
                        session.permanent = True
                        return redirect(url_for('index'))
                    error, status = 'Нэвтрэх нэр эсвэл нууц үг буруу байна.', 401
                except LDAPUnavailable as exc:
                    error, status = str(exc), 503
        return render_template('login.html', error=error), status

    @app.post('/logout')
    def logout():
        token = session.get('csrf_token', '')
        if not token or not hmac.compare_digest(token.encode(), request.form.get('csrf_token', '').encode()):
            return jsonify(message='Хүсэлт зөвшөөрөгдөөгүй.'), 403
        session.clear()
        return redirect(url_for('login'))
