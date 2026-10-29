"""Stateless restart eligibility, shared by UI reads and every mutation path."""
import math
from datetime import datetime, timezone

STABLE_SECONDS = 300


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError('Missing timestamp')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Timestamp must have timezone')
    return parsed


def eligibility(deployment, pods, replicasets, now=None):
    now = now or datetime.now(timezone.utc)

    def blocked(reason, seconds=0):
        return {'allowed': False, 'reason': reason, 'cooldown_seconds': seconds}

    meta, spec, status = deployment.get('metadata', {}), deployment.get('spec', {}), deployment.get('status', {})
    desired = spec.get('replicas', 1)
    if meta.get('deletionTimestamp') or spec.get('paused') or desired < 1:
        return blocked('Deployment зогссон, paused эсвэл replica=0 байна.')
    conditions = {c.get('type'): c for c in status.get('conditions', [])}
    if (conditions.get('ReplicaFailure', {}).get('status') == 'True'
            or conditions.get('Progressing', {}).get('status') == 'False'):
        return blocked('Deployment алдаатай байна.')
    if (status.get('observedGeneration', 0) < meta.get('generation', 1)
            or any(status.get(k, 0) != desired for k in ('replicas', 'updatedReplicas', 'readyReplicas', 'availableReplicas'))
            or status.get('unavailableReplicas', 0) or status.get('terminatingReplicas', 0)
            or conditions.get('Available', {}).get('status') != 'True'
            or conditions.get('Progressing', {}).get('reason') != 'NewReplicaSetAvailable'):
        return blocked('Rollout дуусаагүй эсвэл бүх replica бэлэн болоогүй.')
    uid = meta.get('uid')
    if not uid:
        return blocked('Deployment ownership мэдээлэл дутуу байна.')
    rs_ids = {r.get('metadata', {}).get('uid') for r in replicasets
              if any(o.get('kind') == 'Deployment' and o.get('uid') == uid and o.get('controller') is True
                     for o in r.get('metadata', {}).get('ownerReferences', []))}
    rs_ids.discard(None)
    owned = [p for p in pods if any(o.get('kind') == 'ReplicaSet' and o.get('uid') in rs_ids and o.get('controller') is True
                                   for o in p.get('metadata', {}).get('ownerReferences', []))]
    if len(owned) != desired:
        return blocked('Pod тоо тохирохгүй эсвэл хуучин pod бүрэн зогсоогүй.')
    try:
        times = [timestamp(conditions['Available']['lastTransitionTime']),
                 timestamp(conditions['Progressing']['lastUpdateTime'])]
        restarted_at = spec.get('template', {}).get('metadata', {}).get('annotations', {}).get('kubectl.kubernetes.io/restartedAt')
        if restarted_at:
            times.append(timestamp(restarted_at))
        for pod in owned:
            pmeta, pspec, state = pod.get('metadata', {}), pod.get('spec', {}), pod.get('status', {})
            if pmeta.get('deletionTimestamp') or state.get('phase') != 'Running':
                return blocked('Pod ажиллаж эхлээгүй эсвэл зогсож байна.')
            ready = next((c for c in state.get('conditions', []) if c.get('type') == 'Ready'), {})
            if ready.get('status') != 'True':
                return blocked('Бүх pod Ready болоогүй.')
            times.append(timestamp(ready.get('lastTransitionTime')))
            containers = pspec.get('containers', [])
            if not containers:
                return blocked('Container мэдээлэл дутуу байна.')
            statuses = {s['name']: s for s in state.get('containerStatuses', [])}
            init_statuses = {s['name']: s for s in state.get('initContainerStatuses', [])}
            for definition in containers + pspec.get('initContainers', []):
                is_init = definition not in containers
                item = (init_statuses if is_init else statuses).get(definition['name'], {})
                current = item.get('state', {})
                if is_init and definition.get('restartPolicy') != 'Always':
                    if current.get('terminated', {}).get('exitCode') != 0:
                        return blocked('Init container амжилттай дуусаагүй.')
                    times.append(timestamp(current['terminated'].get('finishedAt')))
                else:
                    if not item.get('ready') or 'running' not in current or item.get('started') is False:
                        return blocked('Container алдаатай, restart хийж байгаа эсвэл Ready биш байна.')
                    times.append(timestamp(current['running'].get('startedAt')))
                finished = item.get('lastState', {}).get('terminated', {}).get('finishedAt')
                if finished:
                    times.append(timestamp(finished))
        seconds = max(0, math.ceil(STABLE_SECONDS - (now - max(times)).total_seconds()))
        if seconds:
            return blocked(f'5 минут тогтвортой ажиллахыг хүлээнэ: {seconds} сек үлдсэн.', seconds)
    except (ValueError, KeyError, TypeError):
        return blocked('Тогтвортой ажиллагааны хугацааг баталгаажуулах мэдээлэл дутуу байна.')
    return {'allowed': True, 'reason': 'Rollout дууссан, бүх pod/container 5 минут тогтвортой.', 'cooldown_seconds': 0}
