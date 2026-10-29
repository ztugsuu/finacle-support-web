from datetime import datetime, timedelta, timezone


def healthy_deployment(name='actual-ac', configmap='fnhttp-ac-env', now=None):
    now = now or datetime.now(timezone.utc)
    old = (now - timedelta(minutes=10)).isoformat()
    deployment = {'metadata': {'name': name, 'uid': name + '-uid', 'resourceVersion': '31', 'generation': 1},
                  'spec': {'replicas': 1, 'template': {'spec': {'containers': [
                      {'name': 'finacle', 'envFrom': [{'configMapRef': {'name': configmap}}]}]}}},
                  'status': {'observedGeneration': 1, 'replicas': 1, 'updatedReplicas': 1,
                             'readyReplicas': 1, 'availableReplicas': 1,
                             'conditions': [{'type': 'Available', 'status': 'True', 'lastTransitionTime': old},
                                            {'type': 'Progressing', 'status': 'True', 'reason': 'NewReplicaSetAvailable', 'lastUpdateTime': old}]}}
    replicaset = {'metadata': {'name': name + '-rs', 'uid': name + '-rs-uid', 'ownerReferences': [
        {'kind': 'Deployment', 'uid': name + '-uid', 'controller': True}]}}
    pod = {'metadata': {'name': name + '-pod', 'uid': name + '-pod-uid', 'ownerReferences': [
        {'kind': 'ReplicaSet', 'uid': name + '-rs-uid', 'controller': True}]},
        'spec': {'containers': [{'name': 'finacle'}]},
        'status': {'phase': 'Running', 'conditions': [{'type': 'Ready', 'status': 'True', 'lastTransitionTime': old}],
                   'containerStatuses': [{'name': 'finacle', 'ready': True, 'restartCount': 0,
                                          'state': {'running': {'startedAt': old}}}]}}
    return deployment, pod, replicaset
