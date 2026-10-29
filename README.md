# Finacle Support Web

Flask dashboard for `pre-uat-finacle` and `uat-finacle`. Select the project in the header. Every OpenShift command explicitly targets the request namespace; changing `oc project` does not change the web selection. Other namespaces are rejected.

## Log sources

| Pod name / prefix | Directory | Files |
| --- | --- | --- |
| `fip`, `fip-*` | `/usr/local/finacle/fe/common/log/fsb/fsbhttp` | `ciErrorLog.log`, `ciDebugLog.log`, `instrumentationLog.log` |
| `fnbrnmpy`, `fnbrnmpy-*` | `/usr/local/finacle/fe/common/log/finint/fcoremb` | `ciErrorLog.log`, `instrumentationLog.log` |

The dashboard discovers actual pod names after rollouts. Select a replica and container if more than one is available. The default is a ready pod and its first container; choose the Finacle container when a sidecar comes first.

Log monitoring refreshes a bounded snapshot every 5 seconds (not a continuous `tail -f` stream). Each read executes `oc -n SELECTED_NAMESPACE exec POD -c CONTAINER -- tail -c 262144 -- ALLOWED_PATH` and returns the requested last 1–2000 lines. Busy files can produce more output between refreshes than the snapshot retains. Search filters the loaded snapshot; download saves that snapshot without the search filter. Empty files and cluster/read errors have separate states. Pod and restart eligibility status refresh every 5 seconds.

## Build and deploy

Run from a workstation with Docker/Podman and an authenticated `oc` client. Confirm that the target cluster is correct before applying:

```sh
oc whoami --show-server
oc get namespace pre-uat-finacle uat-finacle
docker build -t securereg.m-bank.mn:5000/finacle-support-web:1.2 .
docker push securereg.m-bank.mn:5000/finacle-support-web:1.2
oc apply -k manifests
oc -n pre-uat-finacle rollout status deployment/finacle-support-web
oc -n pre-uat-finacle port-forward svc/finacle-support-web 5000:5000
```

Open `http://localhost:5000`. The existing private-registry pull secret named `securereg` must exist in this namespace. Update the image tag in `manifests/deploy.yaml` for subsequent builds. `Dockerfile` and the legacy `ztdockerfile` use the same production configuration. The OpenShift client defaults to 4.14.27 (matching the locally installed client); set `--build-arg OC_VERSION=VERSION` to the version supported by your cluster.

The web Deployment, Service and ServiceAccount live in `pre-uat-finacle`. Namespaced Roles and RoleBindings grant that ServiceAccount access to both projects. `rbac-uat.yaml` binds the PRE-UAT ServiceAccount in UAT; it does not create a second web Deployment. Kustomization deliberately has no namespace transformer: resource namespaces must stay explicit. Do not apply this bundle with an overriding `-n` flag. Applying both Roles requires permission in both namespaces. In the pod, `oc` uses the mounted service-account credentials. No local kubeconfig is built into the image. No public Route is created. For shared browser access, connect the service through your existing authenticated internal ingress/access proxy. The application itself has no login or per-user role separation: anyone with access can read logs and request deployment restarts.

The Roles grant pod/ReplicaSet list/get, container logs, pod exec, and deployment list/get/patch for the existing restart feature. Kubernetes `pods/exec` permission cannot restrict the executable or file path; the application enforces its fixed file allowlist. Deployment restart asks for confirmation in the browser and is limited to deployments returned from the selected namespace. If restart should be disabled operationally, remove deployment `patch` from both Roles.

## Restart eligibility

A Deployment can restart only when its rollout is complete, its owned pods and
all running containers are Ready, and it has been stable for 300 seconds. Pod
ownership is resolved through ReplicaSet controller UIDs, not pod-name guesses.
The cooldown starts from the latest pod Ready transition, container start/recovery,
Deployment availability/rollout completion or restart annotation timestamp.
Completed init containers must have exit code zero; running init sidecars must be
Ready. Historical restart counts do not block forever after five stable minutes.

Missing timestamps/permissions, paused or scaled-down Deployments, old/terminating
pods, failed containers and incomplete rollouts disable restart. The UI refreshes
every five seconds and fails closed if a read fails. The server independently checks
all restart paths, including Log control and reset-all, rechecks after dry-run and
before each restart, and uses resourceVersion checks to reject competing writes.
No per-browser or per-worker cooldown state is trusted.

## Projects

The project picker reloads the page to discard stale data and previews. Mutation
requests require an explicit allowed namespace, and preview fingerprints include
that namespace. Switching projects is disabled during an in-flight mutation. The
`NAMESPACE` environment variable sets the initial project only; both choices remain
available. Local operation uses the logged-in `oc` user's rights in each project.
Rebuild and push the new `1.2` image before deploying the updated manifests.

The container has a 1800-second request/graceful-shutdown allowance for bounded
bulk operations; the web Deployment allows 1830 seconds for termination. Set any
external proxy timeout accordingly. Idle workers still terminate promptly.

## Log control

The sidebar opens separate Overview, Log monitoring, Log control and Deployments
screens, with browser back/forward support. Log snapshots refresh only while the
Log monitoring screen is open.

Open **Log control → ConfigMap унших**, select one ConfigMap and log type,
to immediately read the current logging values. Then preview the current/new values and affected Deployments. Nothing is changed
until **Баталгаажуулж өөрчлөх + Restart** is explicitly confirmed.

Supported ConfigMaps:

- FNHTTP: `fnhttp-ac-env`, `fnhttp-la-env`, `fnhttp-py-env`, `fnhttp-rof-env`.
  Modes: DEBUG, TRACE, NORMAL. USER defaults to `ALL`; a single username is
  supported (letters, digits, underscore, dot, @ and hyphen, up to 128 characters).
- FIP / Finbranch: `fip-env`, `finbranch-fip-*-env`. Modes: DEBUG, NORMAL.
  NORMAL sets `FI_LOGLEVEL=NONE` and `LOG_LEVEL=WARN`; USER does not apply.

**Бүх logging ConfigMap → NORMAL** previews defaults for every allowed logging
ConfigMap in the selected project (up to 10 ConfigMaps and 10 unique Deployments).
It never resets unrelated ConfigMaps or the other project. FNHTTP uses the NORMAL
column below; FIP/Finbranch use `FI_LOGLEVEL=NONE`, `LOG_LEVEL=WARN`. Other keys,
including `TBA_DEBUG_LEVEL` during NORMAL, are preserved. All affected deployments
must pass restart eligibility; all dry-runs must pass before the first real write.
Shared Deployments restart only once. Multi-resource changes are not transactional:
partial failures report completed ConfigMaps and confirmed/pending restarts without
an automatic rollback. A fresh preview is required before retrying.

FNHTTP presets:

| Key | DEBUG | TRACE | NORMAL |
| --- | --- | --- | --- |
| FINACLE_LOG4FIN_ENABLED | N | Y | N |
| DEBUG_LOG_USERS | USER field | USER field | NONE |
| DEBUG_LOG_LEVEL | DEBUG | TRACE | NONE |
| GLOBAL_TRACE | OFF | ON | OFF |
| TBA_DEBUG_LEVEL | 0 | unchanged | unchanged |

The backend discovers Deployments from ConfigMap references in container/init
container environment variables, volumes and projected volumes. It does not infer
Deployment names from ConfigMap names. Missing references and immutable ConfigMaps
block changes. At most 10 Deployments can be restarted per operation.

Preview reads the configuration and runs `oc patch --dry-run=server` for the
ConfigMap and every affected Deployment. Apply rechecks the preview and repeats
all dry-runs before making any real changes. A failed dry-run blocks all writes.
Dry-run validates API authorization/admission, but creates no pods and cannot
prove scheduling, image pulls or application startup. Paused Deployments and
Deployments with zero replicas are blocked before changes.

Apply patches only the specified
log keys while preserving other data, and triggers rolling restarts by updating
the standard `kubectl.kubernetes.io/restartedAt` pod-template annotation.
Resource-version checks reject concurrent changes. Success means restart requests
were accepted, not that rollouts have completed. The UI polls read-only Deployment
status every five seconds for up to ten minutes. It verifies the restart annotation,
observed generation, updated/ready/available replica counts and absence of old or
reported terminating replicas before showing completion. Deadline/replica failures,
external restarts, paused/scaled-down deployments and monitoring timeouts are
shown separately; manual recheck is available. Readiness means Kubernetes readiness,
not an end-to-end Finacle transaction check. Keep the browser open during monitoring.
Partial failures report the ConfigMap update and confirmed/pending restarts;
there is no automatic rollback or automatic retry. After a timeout, read current
ConfigMap values and Deployment status before submitting another change.

The repository RBAC manifest adds ConfigMap read/list and patch rights limited to
the nine known ConfigMaps in each project. Add explicit names to both Roles for new Finbranch ConfigMaps;
RBAC resource names do not support wildcards. This manifest must be deployed by
an authorized operator for in-cluster use. Local execution uses the logged-in
`oc` user's permissions. The app has no user login; use the existing authenticated
internal access proxy for shared access.

## Verification commands

```sh
python -m unittest discover -s tests -v
node --check static/app.js
node tests/browser_smoke.cjs
oc kustomize manifests
```

The browser smoke test uses local mock APIs only and requires Windows, the local
`.venv-local` Python environment and Chrome (override its path with `CHROME_PATH`).
It checks navigation, mobile menus, dry-run failure handling and rollout status UI
without connecting to the cluster.

For local development, install `requirements.txt`, authenticate `oc` to the intended cluster, then run `python app.py`. `/health` checks the web process; `/api/overview` checks cluster access. A failure to read deployments does not hide available pod information.

After deployment, confirm both source tabs show the intended pods, select the correct application container, and open each of the five files. A permission or missing-file error is displayed in the log panel. Use a controlled environment to validate the optional restart action.
