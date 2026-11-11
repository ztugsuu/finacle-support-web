from flask import Flask, render_template, request, jsonify, Response, send_file
import json
from flask import g, has_request_context
from restart_guard import eligibility
import hashlib
import io
import zipfile
import os
import re
import subprocess
from urllib.parse import quote
from datetime import datetime, timezone

app = Flask(__name__)
NAMESPACES = ('pre-uat-finacle', 'uat-finacle')
NAMESPACE = os.environ.get('NAMESPACE', 'pre-uat-finacle')
if NAMESPACE not in NAMESPACES:
    raise RuntimeError('NAMESPACE must be pre-uat-finacle or uat-finacle')


def current_namespace():
    return g.namespace if has_request_context() else NAMESPACE


@app.before_request
def select_namespace():
    header, query = request.headers.get('X-Finacle-Namespace'), request.args.get('namespace')
    if header and query and header != query:
        return jsonify(message='Project сонголтууд зөрж байна.'), 400
    selected = header or query or NAMESPACE
    if selected not in NAMESPACES:
        return jsonify(message='Project зөвшөөрөгдөөгүй.'), 400
    if request.method == 'POST' and not (header or query):
        return jsonify(message='Project-ийг тодорхой сонгоно уу.'), 400
    g.namespace = selected
SOURCES = {
    'fnhttp': {'path': '/usr/local/finacle/be/CDCI_LOGS/FIVUSR', 'files': [], 'dynamic': True},
    'fip': {'path': '/usr/local/finacle/fe/common/log/fsb/fsbhttp',
            'files': ['ciErrorLog.log', 'ciDebugLog.log', 'instrumentationLog.log']},
    'fnbrnmpy': {'path': '/usr/local/finacle/fe/common/log/finint/fcoremb',
                'files': ['ciErrorLog.log', 'instrumentationLog.log']},
}
MAX_BYTES = 262144
FNHTTP_CONFIGS = {'fnhttp-ac-env', 'fnhttp-la-env', 'fnhttp-py-env', 'fnhttp-rof-env'}
LOG_KEYS = ('FINACLE_LOG4FIN_ENABLED', 'DEBUG_LOG_USERS', 'DEBUG_LOG_LEVEL',
            'GLOBAL_TRACE', 'TBA_DEBUG_LEVEL', 'FI_LOGLEVEL', 'LOG_LEVEL')


def log_group(name):
    if name in FNHTTP_CONFIGS:
        return 'FNHTTP'
    if name == 'fip-env':
        return 'FIP'
    if re.fullmatch(r'finbranch-fip-[a-z0-9][a-z0-9.-]*-env', name):
        return 'Finbranch'
    return None


def config_references(deployment):
    spec = deployment.get('spec', {}).get('template', {}).get('spec', {})
    names = set()
    for container in spec.get('containers', []) + spec.get('initContainers', []):
        for entry in container.get('envFrom', []):
            name = entry.get('configMapRef', {}).get('name')
            if name:
                names.add(name)
        for entry in container.get('env', []):
            name = entry.get('valueFrom', {}).get('configMapKeyRef', {}).get('name')
            if name:
                names.add(name)
    for volume in spec.get('volumes', []):
        if volume.get('configMap', {}).get('name'):
            names.add(volume['configMap']['name'])
        for entry in volume.get('projected', {}).get('sources', []):
            if entry.get('configMap', {}).get('name'):
                names.add(entry['configMap']['name'])
    return names


def logging_values(group, mode, username):
    if mode not in ('DEBUG', 'TRACE', 'NORMAL') or (group != 'FNHTTP' and mode == 'TRACE'):
        raise ValueError('Log type сонголт буруу байна.')
    if group != 'FNHTTP':
        return {'FI_LOGLEVEL': 'DEBUG' if mode == 'DEBUG' else 'NONE',
                'LOG_LEVEL': 'DEBUG' if mode == 'DEBUG' else 'WARN'}
    if mode == 'NORMAL':
        return {'FINACLE_LOG4FIN_ENABLED': 'N', 'DEBUG_LOG_USERS': 'NONE',
                'DEBUG_LOG_LEVEL': 'NONE', 'GLOBAL_TRACE': 'OFF'}
    if not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9_.@-]{1,128}', username):
        raise ValueError('USER: ALL эсвэл нэг USERNAME оруулна уу (1–128 тэмдэгт).')
    if mode == 'TRACE':
        return {'FINACLE_LOG4FIN_ENABLED': 'Y', 'GLOBAL_TRACE': 'ON',
                'DEBUG_LOG_LEVEL': 'TRACE', 'DEBUG_LOG_USERS': username}
    return {'FINACLE_LOG4FIN_ENABLED': 'N', 'DEBUG_LOG_USERS': username,
            'DEBUG_LOG_LEVEL': 'DEBUG', 'GLOBAL_TRACE': 'OFF', 'TBA_DEBUG_LEVEL': '0'}


def logging_plan(body, config=None, deployment_list=None):
    name = body.get('configmap')
    group = log_group(name) if isinstance(name, str) else None
    if not group:
        raise ValueError('ConfigMap сонголт зөвшөөрөгдөөгүй.')
    values = logging_values(group, body.get('mode'), body.get('username', 'ALL'))
    config = config if config is not None else json.loads(oc('get', 'configmap', name, '-o', 'json'))
    if config.get('immutable'):
        raise ValueError('Энэ ConfigMap immutable тул өөрчлөх боломжгүй.')
    linked = [d for d in (deployment_list if deployment_list is not None else resources('deployments')) if name in config_references(d)]
    if any(d.get('spec', {}).get('paused') or d.get('spec', {}).get('replicas', 1) == 0 for d in linked):
        raise ValueError('Deployment paused эсвэл replica=0 байна. Эхлээд Deployment төлөвийг шалгана уу.')
    deployments = sorted(({'name': d['metadata']['name'],
                           'version': d['metadata']['resourceVersion']}
                          for d in linked),
                         key=lambda d: d['name'])
    if not deployments:
        raise ValueError('Энэ ConfigMap-ийг ашигладаг Deployment олдсонгүй. Өөрчлөлт хийхгүй.')
    if len(deployments) > 10:
        raise ValueError('10-аас олон Deployment холбогдсон байна. Энэ цонхоор өөрчлөх боломжгүй.')
    current = config.get('data', {})
    plan = {'namespace': current_namespace(), 'configmap': name, 'mode': body.get('mode'),
            'version': config['metadata']['resourceVersion'], 'deployments': deployments,
            'changes': [{'key': key, 'before': current.get(key), 'after': value}
                        for key, value in values.items()]}
    plan['fingerprint'] = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    return plan, config, values


def logging_patches(plan, config, values, restarted_at):
    config_patch = [{'op': 'test', 'path': '/metadata/resourceVersion', 'value': plan['version']},
                    {'op': 'add', 'path': '/data', 'value': {**config.get('data', {}), **values}}]
    commands = [('patch', 'configmap', plan['configmap'], '--type=json', '-p', json.dumps(config_patch))]
    for deployment in plan['deployments']:
        restart_patch = {'metadata': {'resourceVersion': deployment['version']},
                         'spec': {'template': {'metadata': {'annotations': {
                             'kubectl.kubernetes.io/restartedAt': restarted_at}}}}}
        commands.append(('patch', 'deployment', deployment['name'], '--type=merge', '-p', json.dumps(restart_patch)))
    return commands


def rollout_state(deployment, restarted_at):
    meta, spec, status = deployment['metadata'], deployment.get('spec', {}), deployment.get('status', {})
    desired = spec.get('replicas', 1)
    updated, ready = status.get('updatedReplicas', 0), status.get('readyReplicas', 0)
    annotation = spec.get('template', {}).get('metadata', {}).get('annotations', {}).get('kubectl.kubernetes.io/restartedAt')
    observed = status.get('observedGeneration', 0) >= meta.get('generation', 1)
    state, message = 'progressing', 'Шинэ pod-ууд бэлэн болохыг хүлээж байна.'
    if annotation != restarted_at:
        state, message = 'changed', 'Өөр restart эсвэл template өөрчлөлт илэрлээ. Deployment-ийг шалгана уу.'
    elif spec.get('paused') or desired == 0:
        state, message = 'blocked', 'Deployment paused эсвэл replica=0; pod бэлэн болсон гэж үзэхгүй.'
    elif observed and any(c.get('type') == 'Progressing' and c.get('status') == 'False'
                          and c.get('reason') == 'ProgressDeadlineExceeded' for c in status.get('conditions', [])):
        state, message = 'failed', 'Pod-ууд хугацаандаа бэлэн болсонгүй (ProgressDeadlineExceeded).'
    elif observed and any(c.get('type') == 'ReplicaFailure' and c.get('status') == 'True' for c in status.get('conditions', [])):
        state, message = 'failed', 'Replica үүсгэх алдаа. Pod болон Deployment event-үүдийг шалгана уу.'
    elif (observed and updated == desired and ready == desired
          and status.get('availableReplicas', 0) == desired and status.get('replicas', 0) == desired
          and status.get('unavailableReplicas', 0) == 0 and status.get('terminatingReplicas', 0) == 0):
        state, message = 'ready', 'Шинэ replica-ууд бүгд Ready / Available.'
    return {'name': meta['name'], 'desired': desired, 'updated': updated, 'ready': ready,
            'state': state, 'message': message}


class ClusterError(Exception):
    pass


class RestartBlocked(Exception):
    pass


def check_restart_targets(targets):
    deployments = {d['metadata']['name']: d for d in resources('deployments')}
    pod_items, replicasets = resources('pods'), resources('replicasets')
    for target in targets:
        deployment = deployments.get(target['name'])
        if deployment is None:
            raise RestartBlocked(f'{target["name"]}: Deployment олдсонгүй.')
        if target.get('version') and deployment['metadata']['resourceVersion'] != target['version']:
            raise RestartBlocked(f'{target["name"]}: төлөв өөрчлөгдсөн. Дахин шалгана уу.')
        state = eligibility(deployment, pod_items, replicasets)
        if not state['allowed']:
            raise RestartBlocked(f'{target["name"]}: {state["reason"]}')
    return deployments


@app.errorhandler(RestartBlocked)
def restart_blocked(error):
    return jsonify(message=str(error)), 409


def oc_bytes(*args):
    try:
        result = subprocess.run(['oc', '-n', current_namespace(), '--request-timeout=15s', *args],
                                capture_output=True, timeout=20, check=False)
    except subprocess.TimeoutExpired as exc:
        raise ClusterError('Cluster хариу өгөх хугацаа хэтэрлээ. Дахин оролдоно уу.') from exc
    except OSError as exc:
        app.logger.error('oc unavailable: %s', exc)
        raise ClusterError('OpenShift client ажиллахгүй байна.') from exc
    if result.returncode:
        error = result.stderr.decode('utf-8', errors='replace').strip()
        app.logger.warning('oc failed: %s', error)
        raise ClusterError(error[:2000] or 'OpenShift хүсэлт амжилтгүй боллоо.')
    return result.stdout


def oc(*args):
    return oc_bytes(*args).decode('utf-8', errors='replace')


def resources(kind):
    try:
        return json.loads(oc('get', kind, '-o', 'json'))['items']
    except (ValueError, KeyError, TypeError) as exc:
        raise ClusterError('Cluster-ээс буруу бүтэцтэй хариу ирлээ.') from exc


def source_for(name):
    return next((key for key in SOURCES if key != 'fnhttp' and (name == key or name.startswith(key + '-'))), None)


def pods(items=None):
    result = []
    items = items if items is not None else resources('pods')
    fnhttp_owners = set()
    if any(p['metadata']['name'].startswith('fnhttp') for p in items):
        deployment_uids = {d['metadata']['uid'] for d in resources('deployments')
                           if d['metadata']['name'].startswith('fnhttp')}
        fnhttp_owners = {r['metadata']['uid'] for r in resources('replicasets')
                         if any(o.get('controller') and o.get('kind') == 'Deployment'
                                and o.get('uid') in deployment_uids
                                for o in r['metadata'].get('ownerReferences', []))}
    for pod in items:
        meta, spec, status = pod['metadata'], pod['spec'], pod.get('status', {})
        statuses = status.get('containerStatuses', [])
        containers = [item['name'] for item in spec.get('containers', [])]
        ready = sum(bool(item.get('ready')) for item in statuses)
        phase = status.get('phase', 'Unknown')
        terminating = bool(meta.get('deletionTimestamp'))
        source = source_for(meta['name'])
        if meta['name'].startswith('fnhttp') and any(
                o.get('controller') and o.get('kind') == 'ReplicaSet' and o.get('uid') in fnhttp_owners
                for o in meta.get('ownerReferences', [])):
            source = 'fnhttp'
        result.append({'name': meta['name'], 'source': source,
                       'fnhttp_service': next((key for key in ('ac', 'la', 'py', 'rf')
                                                if meta['name'].startswith('fnhttp' + key + '-')), None) if source == 'fnhttp' else None,
                       'containers': containers, 'ready': ready, 'total': len(containers),
                       'healthy': phase == 'Running' and ready == len(containers) and not terminating,
                       'phase': 'Terminating' if terminating else phase,
                       'restarts': sum(item.get('restartCount', 0) for item in statuses)})
    return sorted(result, key=lambda item: item['name'])


@app.errorhandler(ClusterError)
def cluster_error(error):
    return jsonify(message=str(error)), 502


@app.after_request
def response_headers(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    return response


@app.get('/')
def index():
    return render_template('index.html', namespace=current_namespace(), namespaces=NAMESPACES, sources=SOURCES)


@app.get('/api/overview')
def overview():
    raw_pods = resources('pods')
    pod_list = pods(raw_pods)
    deployments, warning = [], None
    try:
        deployment_list = resources('deployments')
        try:
            replicasets = resources('replicasets')
            guard_error = None
        except ClusterError as exc:
            replicasets, guard_error = [], str(exc)
        for item in deployment_list:
            deployments.append({'name': item['metadata']['name'],
                                'desired': item['spec'].get('replicas', 1),
                                'ready': item.get('status', {}).get('readyReplicas', 0),
                                'restart': eligibility(item, raw_pods, replicasets) if not guard_error else
                                {'allowed': False, 'reason': f'Restart төлөв баталгаажаагүй: {guard_error}', 'cooldown_seconds': 0}})
    except ClusterError as exc:
        warning = str(exc)
    return jsonify(namespace=current_namespace(), pods=pod_list, deployments=deployments,
                   warning=warning, updated_at=datetime.now(timezone.utc).isoformat())


def valid_log_filename(name):
    return (isinstance(name, str) and 0 < len(name) <= 255 and name not in ('.', '..')
            and not any(c in '/\\' or ord(c) < 32 or ord(c) == 127 for c in name))


def log_container(pod):
    if pod['source'] == 'fnhttp':
        expected = 'fnhttp' + (pod.get('fnhttp_service') or '')
        requested = request.args.get('container')
        return expected if not requested or requested == expected else None
    return request.args.get('container') or pod['containers'][0]


def selected_log_pod(source):
    candidates = [p for p in pods() if p['source'] == source
                  and (not request.args.get('pod') or p['name'] == request.args['pod'])
                  and (source != 'fnhttp' or not request.args.get('service')
                       or p.get('fnhttp_service') == request.args['service'])]
    candidates.sort(key=lambda p: (not p['healthy'], p['name']))
    return candidates[0] if candidates else None


@app.get('/api/log-files')
def log_files():
    if request.args.get('source') != 'fnhttp':
        return jsonify(message='Log эх үүсвэр буруу байна.'), 400
    pod = selected_log_pod('fnhttp')
    if not pod:
        return jsonify(message='FNHTTP deployment-ийн pod олдсонгүй.'), 404
    if pod['phase'] != 'Running':
        return jsonify(message='Сонгосон pod ажиллаж эхлээгүй байна.'), 409
    container = log_container(pod)
    if container not in pod['containers']:
        return jsonify(message='Container сонголт буруу байна.'), 400
    # The script is constant; the directory is passed as an argument, never interpolated.
    output = oc('exec', pod['name'], '-c', container, '--', 'sh', '-c',
                'if [ -d "$1" ]; then find "$1" -maxdepth 1 -type f '
                "-exec stat --printf='%W\\0%Y\\0%s\\0%n\\0' -- {} +; fi",
                'list-logs', SOURCES['fnhttp']['path'])
    prefix = SOURCES['fnhttp']['path'] + '/'
    entries = output.split('\0')
    if entries[-1] == '':
        entries.pop()
    if len(entries) % 4:
        raise ClusterError('Файлын цагийн мэдээллийг уншиж чадсангүй.')
    details = []
    for index in range(0, len(entries), 4):
        birth, modified, size, path = entries[index:index + 4]
        if not path.startswith(prefix) or not valid_log_filename(path[len(prefix):]):
            continue
        try:
            birth, modified, size = int(birth), int(modified), int(size)
            if size < 0:
                raise ValueError('negative file size')
            timestamp = birth if birth > 0 else modified
            display_time = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError) as exc:
            raise ClusterError('Файлын цагийн мэдээлэл буруу байна.') from exc
        details.append({'name': path[len(prefix):], 'size_bytes': size, 'timestamp': timestamp,
                        'time': display_time, 'time_kind': 'created' if birth > 0 else 'modified'})
    details.sort(key=lambda item: (-item['timestamp'], item['name']))
    return jsonify(files=[item['name'] for item in details], file_details=details,
                   pod=pod['name'], container=container)


@app.post('/api/logs/download')
def download_log_files():
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or request.headers.get('X-Requested-With') != 'FinacleSupport':
        return jsonify(message='Хүсэлт зөвшөөрөгдөөгүй.'), 400
    source = request.args.get('source')
    names = body.get('files')
    if (source not in SOURCES or not isinstance(names, list) or not 1 <= len(names) <= 100
            or any(not valid_log_filename(name) for name in names)):
        return jsonify(message='1–100 файл сонгоно уу.'), 400
    names = list(dict.fromkeys(names))
    if source != 'fnhttp' and any(name not in SOURCES[source]['files'] for name in names):
        return jsonify(message='Файлын сонголт зөвшөөрөгдөөгүй.'), 400
    pod = selected_log_pod(source)
    if not pod:
        return jsonify(message='Pod олдсонгүй.'), 404
    if pod['phase'] != 'Running':
        return jsonify(message='Сонгосон pod ажиллаж эхлээгүй байна.'), 409
    container = log_container(pod)
    if container not in pod['containers']:
        return jsonify(message='Container сонголт буруу байна.'), 400
    archive = io.BytesIO()
    remaining = 100 * 1000000
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:
        for name in names:
            path = SOURCES[source]['path'] + '/' + name
            # Reject symlinks/non-files. Bound the read without truncating a successful download.
            data = oc_bytes('exec', pod['name'], '-c', container, '--', 'sh', '-c',
                            'test -f "$1" && test ! -L "$1" && head -c "$2" -- "$1"',
                            'download-log', path, str(remaining + 1))
            if len(data) > remaining:
                return jsonify(message='Сонгосон файлуудын нийт хэмжээ 100 MB-аас хэтэрсэн байна. Цөөн файл сонгоно уу.'), 413
            remaining -= len(data)
            bundle.writestr(name, data)
    archive.seek(0)
    return send_file(archive, mimetype='application/zip', as_attachment=True,
                     download_name=f"{pod['name']}-logs.zip")


@app.get('/api/logs')
def file_logs():
    source, filename = request.args.get('source'), request.args.get('file')
    if source not in SOURCES or not (valid_log_filename(filename) if source == 'fnhttp' else filename in SOURCES[source]['files']):
        return jsonify(message='Зөвшөөрөгдсөн log файл сонгоно уу.'), 400
    try:
        lines = int(request.args.get('lines', '500'))
        if not 1 <= lines <= 2000:
            raise ValueError()
    except ValueError:
        return jsonify(message='Мөрийн тоо 1–2000 хооронд байна.'), 400
    pod = selected_log_pod(source)
    if not pod:
        return jsonify(message=f'{source} pod олдсонгүй. Pod жагсаалтыг шинэчилнэ үү.'), 404
    if pod['phase'] != 'Running':
        return jsonify(message='Сонгосон pod ажиллаж эхлээгүй байна.'), 409
    container = log_container(pod)
    if container not in pod['containers']:
        return jsonify(message='Container сонголт буруу байна.'), 400
    path = SOURCES[source]['path'] + '/' + filename
    # Fixed allowlist and argv execution: no shell, arbitrary paths or commands.
    try:
        content = oc('exec', pod['name'], '-c', container, '--', 'tail', '-c', str(MAX_BYTES), '--', path)
    except ClusterError as exc:
        if source == 'fnhttp' and 'No such file or directory' in str(exc):
            return jsonify(message='Энэ pod/container дотор сонгосон log файл байхгүй байна.'), 404
        raise
    truncated = len(content.encode('utf-8')) >= MAX_BYTES
    if truncated:
        content = content.partition('\n')[2]
    content = '\n'.join(content.splitlines()[-lines:])
    if request.args.get('download') == '1':
        return Response(content, mimetype='text/plain', headers={
            'Content-Disposition': f"attachment; filename*=UTF-8''{quote(source + '-' + filename, safe='')}"})
    return jsonify(content=content, pod=pod['name'], container=container, path=path,
                   truncated=truncated, updated_at=datetime.now(timezone.utc).isoformat())


@app.get('/logs/<pod_name>')
def stdout_logs(pod_name):
    if pod_name not in [pod['name'] for pod in pods()]:
        return jsonify(message='Pod олдсонгүй.'), 404
    return Response(oc('logs', pod_name, '--tail=500', '--limit-bytes=262144'), mimetype='text/plain')


@app.post('/restart')
def restart():
    # A custom header and JSON prevent cross-origin form-based restart requests.
    if request.headers.get('X-Requested-With') != 'FinacleSupport' or not request.is_json:
        return jsonify(message='Хүсэлт зөвшөөрөгдөөгүй.'), 403
    data = request.get_json(silent=True)
    name = data.get('name') if isinstance(data, dict) else None
    if not isinstance(name, str) or not re.fullmatch(r'[a-z0-9][a-z0-9.-]*', name):
        return jsonify(message='Deployment нэр буруу байна.'), 400
    deployments = {item['metadata']['name']: item for item in resources('deployments')}
    if name not in deployments:
        return jsonify(message='Deployment олдсонгүй.'), 404
    version = deployments[name]['metadata']['resourceVersion']
    check_restart_targets([{'name': name, 'version': version}])
    # ResourceVersion makes competing requests conflict, including across workers.
    patch = {'metadata': {'resourceVersion': version}, 'spec': {'template': {'metadata': {'annotations': {
        'kubectl.kubernetes.io/restartedAt': datetime.now(timezone.utc).isoformat()}}}}}
    oc('patch', 'deployment', name, '--type=merge', '-p', json.dumps(patch))
    return jsonify(message=f'{name}: restart хүсэлт илгээгдлээ. Төлөвийг хянана уу.')


@app.get('/health')
def health():
    return jsonify(status='UP')


@app.get('/api/log-control')
def log_control_list():
    configs = []
    for config in resources('configmaps'):
        name = config['metadata']['name']
        group = log_group(name)
        if group:
            configs.append({'name': name, 'group': group,
                            'values': {k: v for k, v in config.get('data', {}).items() if k in LOG_KEYS}})
    return jsonify(configmaps=sorted(configs, key=lambda c: c['name']))


@app.get('/api/log-control/current')
def log_control_current():
    name = request.args.get('configmap', '')
    if not log_group(name):
        return jsonify(message='ConfigMap сонголт зөвшөөрөгдөөгүй.'), 400
    config = json.loads(oc('get', 'configmap', name, '-o', 'json'))
    return jsonify(name=name, namespace=current_namespace(),
                   values={k: v for k, v in config.get('data', {}).items() if k in LOG_KEYS})


@app.post('/api/log-control/<action>')
def log_control_action(action):
    if action not in ('preview', 'apply', 'reset-preview', 'reset-apply'):
        return jsonify(message='Үйлдэл олдсонгүй.'), 404
    if request.headers.get('X-Requested-With') != 'FinacleSupport' or not request.is_json:
        return jsonify(message='Хүсэлт зөвшөөрөгдөөгүй.'), 403
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(message='JSON хүсэлт буруу байна.'), 400
    applying = action in ('apply', 'reset-apply')
    reset_all = action.startswith('reset-')
    if applying and (body.get('confirmed') is not True or not body.get('fingerprint')):
        return jsonify(message='Эхлээд өөрчлөлтийг урьдчилан харж баталгаажуулна уу.'), 400
    try:
        if reset_all:
            configs = sorted((c for c in resources('configmaps') if log_group(c['metadata']['name'])),
                             key=lambda c: c['metadata']['name'])
            if not configs or len(configs) > 10:
                raise ValueError('Reset нь сонгосон project-ийн 1–10 logging ConfigMap-д үйлчилнэ.')
            deployment_list = resources('deployments')
            entries = [logging_plan({'configmap': c['metadata']['name'], 'mode': 'NORMAL'}, c, deployment_list) for c in configs]
        else:
            entries = [logging_plan(body)]
    except ValueError as exc:
        return jsonify(message=str(exc)), 400
    targets = {d['name']: d for plan, _, _ in entries for d in plan['deployments']}
    if len(targets) > 10:
        return jsonify(message='Нэг үйлдэлд 10-аас олон Deployment restart хийхгүй.'), 400
    plans = [entry[0] for entry in entries]
    if reset_all:
        plan = {'namespace': current_namespace(), 'mode': 'NORMAL', 'reset_all': True,
                'configurations': plans, 'deployments': sorted(targets.values(), key=lambda d: d['name'])}
        plan['fingerprint'] = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    else:
        plan = plans[0]
    if applying and body['fingerprint'] != plan['fingerprint']:
        return jsonify(message='ConfigMap эсвэл Deployment өөрчлөгдсөн байна. Дахин урьдчилан харна уу.'), 409
    check_restart_targets(list(targets.values()))
    restarted_at = datetime.now(timezone.utc).isoformat()
    config_commands, restart_commands = [], {}
    for entry, config, values in entries:
        commands = logging_patches(entry, config, values, restarted_at)
        config_commands.append(commands[0])
        restart_commands.update({c[2]: c for c in commands[1:]})
    # Validate EVERY proposed write first. Server-side dry-run persists nothing
    # and does not create pods or prove application startup/readiness.
    try:
        for command in config_commands + list(restart_commands.values()):
            oc(*command, '--dry-run=server')
    except ClusterError as exc:
        return jsonify(message=f'Dry-run амжилтгүй: {exc}. ConfigMap өөрчлөөгүй, restart илгээгээгүй.'), 502
    if not applying:
        return jsonify(**plan, dry_run=True)
    # Re-read pod/container readiness after dry-run, before the first real write.
    check_restart_targets(list(targets.values()))
    updated, restarted = [], []
    tracking = {'namespace': current_namespace(), 'configmap': '*' if reset_all else plan['configmap'],
                'restarted_at': restarted_at, 'names': []}
    for command in config_commands:
        try:
            oc(*command)
            updated.append(command[2])
        except ClusterError as exc:
            return jsonify(message=f'ConfigMap update баталгаажсан: {", ".join(updated) or "байхгүй"}. '
                                   f'{command[2]} update баталгаажаагүй: {exc}. Restart илгээгээгүй. '
                                   'Утгуудыг дахин уншиж шалгана уу; автоматаар буцаагаагүй.',
                           updated=updated, restarted=[]), 502
    for name, command in restart_commands.items():
        try:
            check_restart_targets([targets[name]])
            tracking['names'].append(name)
            oc(*command)
            restarted.append(name)
        except (ClusterError, RestartBlocked) as exc:
            pending = [n for n in targets if n not in restarted]
            return jsonify(message=f'ConfigMap шинэчлэгдсэн. Restart баталгаажсан: {", ".join(restarted) or "байхгүй"}. '
                                   f'Restart баталгаажаагүй: {", ".join(pending)}. {exc}. '
                                   'Deployment төлөвийг шалгана уу; өөрчлөлтийг автоматаар буцаагаагүй.',
                           configmap_updated=True, updated=updated, restarted=restarted, pending=pending,
                           tracking=tracking if tracking['names'] else None), 502
    return jsonify(message=f'{", ".join(updated)}: {plan["mode"]} тохирууллаа. '
                           f'Restart хүсэлт илгээсэн: {", ".join(restarted)}. Deployment төлөвийг хянана уу.',
                   configmap_updated=True, updated=updated, restarted=restarted, tracking=tracking)


@app.get('/api/log-control/rollout')
def log_control_rollout():
    configmap = request.args.get('configmap', '')
    restarted_at = request.args.get('restarted_at', '')
    names = request.args.get('names', '').split(',')
    if ((configmap != '*' and not log_group(configmap)) or not restarted_at or len(restarted_at) > 64
            or not 1 <= len(names) <= 10
            or any(not re.fullmatch(r'[a-z0-9][a-z0-9.-]*', name) for name in names)):
        return jsonify(message='Rollout хүсэлт буруу байна.'), 400
    deployments = {d['metadata']['name']: d for d in resources('deployments')}
    result = []
    for name in names:
        deployment = deployments.get(name)
        references = config_references(deployment) if deployment else set()
        linked = any(log_group(n) for n in references) if configmap == '*' else configmap in references
        if not deployment or not linked:
            result.append({'name': name, 'state': 'changed', 'desired': None, 'updated': None, 'ready': None,
                           'message': 'Deployment олдсонгүй эсвэл ConfigMap холбоос өөрчлөгдсөн.'})
        else:
            result.append(rollout_state(deployment, restarted_at))
    return jsonify(deployments=result, complete=all(d['state'] == 'ready' for d in result))


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
