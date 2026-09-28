from flask import Flask, render_template, request, jsonify, Response
import json
import os
import re
import subprocess
from datetime import datetime, timezone

app = Flask(__name__)
NAMESPACE = 'pre-uat-finacle'
SOURCES = {
    'fip': {'path': '/usr/local/finacle/fe/common/log/fsb/fsbhttp',
            'files': ['ciErrorLog.log', 'ciDebugLog.log', 'instrumentationLog.log']},
    'fnbrnmpy': {'path': '/usr/local/finacle/fe/common/log/finint/fcoremb',
                'files': ['ciErrorLog.log', 'instrumentationLog.log']},
}
MAX_BYTES = 262144


class ClusterError(Exception):
    pass


def oc(*args):
    try:
        result = subprocess.run(['oc', '-n', NAMESPACE, '--request-timeout=15s', *args],
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
    return result.stdout.decode('utf-8', errors='replace')


def resources(kind):
    try:
        return json.loads(oc('get', kind, '-o', 'json'))['items']
    except (ValueError, KeyError, TypeError) as exc:
        raise ClusterError('Cluster-ээс буруу бүтэцтэй хариу ирлээ.') from exc


def source_for(name):
    return next((key for key in SOURCES if name == key or name.startswith(key + '-')), None)


def pods():
    result = []
    for pod in resources('pods'):
        meta, spec, status = pod['metadata'], pod['spec'], pod.get('status', {})
        statuses = status.get('containerStatuses', [])
        containers = [item['name'] for item in spec.get('containers', [])]
        ready = sum(bool(item.get('ready')) for item in statuses)
        phase = status.get('phase', 'Unknown')
        terminating = bool(meta.get('deletionTimestamp'))
        result.append({'name': meta['name'], 'source': source_for(meta['name']),
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
    return render_template('index.html', namespace=NAMESPACE, sources=SOURCES)


@app.get('/api/overview')
def overview():
    pod_list = pods()
    deployments, warning = [], None
    try:
        for item in resources('deployments'):
            deployments.append({'name': item['metadata']['name'],
                                'desired': item['spec'].get('replicas', 1),
                                'ready': item.get('status', {}).get('readyReplicas', 0)})
    except ClusterError as exc:
        warning = str(exc)
    return jsonify(namespace=NAMESPACE, pods=pod_list, deployments=deployments,
                   warning=warning, updated_at=datetime.now(timezone.utc).isoformat())


@app.get('/api/logs')
def file_logs():
    source, filename = request.args.get('source'), request.args.get('file')
    if source not in SOURCES or filename not in SOURCES[source]['files']:
        return jsonify(message='Зөвшөөрөгдсөн log файл сонгоно уу.'), 400
    try:
        lines = int(request.args.get('lines', '500'))
        if not 1 <= lines <= 2000:
            raise ValueError()
    except ValueError:
        return jsonify(message='Мөрийн тоо 1–2000 хооронд байна.'), 400
    candidates = [pod for pod in pods() if pod['source'] == source]
    requested = request.args.get('pod')
    if requested:
        candidates = [pod for pod in candidates if pod['name'] == requested]
    candidates.sort(key=lambda item: (not item['healthy'], item['name']))
    if not candidates:
        return jsonify(message=f'{source} pod олдсонгүй. Pod жагсаалтыг шинэчилнэ үү.'), 404
    pod = candidates[0]
    if pod['phase'] != 'Running':
        return jsonify(message='Сонгосон pod ажиллаж эхлээгүй байна.'), 409
    container = request.args.get('container') or pod['containers'][0]
    if container not in pod['containers']:
        return jsonify(message='Container сонголт буруу байна.'), 400
    path = SOURCES[source]['path'] + '/' + filename
    # Fixed allowlist and argv execution: no shell, arbitrary paths or commands.
    content = oc('exec', pod['name'], '-c', container, '--', 'tail', '-c', str(MAX_BYTES), '--', path)
    truncated = len(content.encode('utf-8')) >= MAX_BYTES
    if truncated:
        content = content.partition('\n')[2]
    content = '\n'.join(content.splitlines()[-lines:])
    if request.args.get('download') == '1':
        return Response(content, mimetype='text/plain', headers={
            'Content-Disposition': f'attachment; filename="{source}-{filename}"'})
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
    if name not in [item['metadata']['name'] for item in resources('deployments')]:
        return jsonify(message='Deployment олдсонгүй.'), 404
    oc('rollout', 'restart', f'deployment/{name}')
    return jsonify(message=f'{name}: restart хүсэлт илгээгдлээ. Төлөвийг хянана уу.')


@app.get('/health')
def health():
    return jsonify(status='UP')


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
