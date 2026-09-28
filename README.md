# Finacle Support Web

Flask dashboard for the `pre-uat-finacle` namespace. All OpenShift commands explicitly target this namespace.

## Log sources

| Pod name / prefix | Directory | Files |
| --- | --- | --- |
| `fip`, `fip-*` | `/usr/local/finacle/fe/common/log/fsb/fsbhttp` | `ciErrorLog.log`, `ciDebugLog.log`, `instrumentationLog.log` |
| `fnbrnmpy`, `fnbrnmpy-*` | `/usr/local/finacle/fe/common/log/finint/fcoremb` | `ciErrorLog.log`, `instrumentationLog.log` |

The dashboard discovers actual pod names after rollouts. Select a replica and container if more than one is available. The default is a ready pod and its first container; choose the Finacle container when a sidecar comes first.

Log monitoring refreshes a bounded snapshot every 5 seconds (not a continuous `tail -f` stream). Each read executes `oc -n pre-uat-finacle exec POD -c CONTAINER -- tail -c 262144 -- ALLOWED_PATH` and returns the requested last 1–2000 lines. Busy files can produce more output between refreshes than the snapshot retains. Search filters the loaded snapshot; download saves that snapshot without the search filter. Empty files and cluster/read errors have separate states. Pod status refreshes every 30 seconds.

## Build and deploy

Run from a workstation with Docker/Podman and an authenticated `oc` client. Confirm that the target cluster is correct before applying:

```sh
oc whoami --show-server
oc get namespace pre-uat-finacle
docker build -t securereg.m-bank.mn:5000/finacle-support-web:1.0 .
docker push securereg.m-bank.mn:5000/finacle-support-web:1.0
oc apply -k manifests
oc -n pre-uat-finacle rollout status deployment/finacle-support-web
oc -n pre-uat-finacle port-forward svc/finacle-support-web 5000:5000
```

Open `http://localhost:5000`. The existing private-registry pull secret named `registry` must exist in this namespace. Update the image tag in `manifests/deploy.yaml` for subsequent builds. `Dockerfile` and the legacy `ztdockerfile` use the same production configuration. The OpenShift client defaults to 4.14.27 (matching the locally installed client); set `--build-arg OC_VERSION=VERSION` to the version supported by your cluster.

Manifests include a namespace-scoped ServiceAccount, Role, RoleBinding, Deployment and internal ClusterIP Service. In the pod, `oc` uses the mounted service-account credentials. No local kubeconfig is built into the image. No public Route is created. For shared browser access, connect the service through your existing authenticated internal ingress/access proxy. The application itself has no login or per-user role separation: anyone with access can read logs and request deployment restarts.

The Role grants pod list/get, container logs, pod exec, and deployment list/get/patch for the existing restart feature. Kubernetes `pods/exec` permission cannot restrict the executable or file path; the application enforces its fixed file allowlist. Deployment restart asks for confirmation in the browser and is limited to deployments returned from this namespace. If restart should be disabled operationally, remove `patch` from the Role.

## Verification

```sh
python -m unittest discover -s tests -v
node --check static/app.js
oc kustomize manifests
```

For local development, install `requirements.txt`, authenticate `oc` to the intended cluster, then run `python app.py`. `/health` checks the web process; `/api/overview` checks cluster access. A failure to read deployments does not hide available pod information.

After deployment, confirm both source tabs show the intended pods, select the correct application container, and open each of the five files. A permission or missing-file error is displayed in the log panel. Use a controlled environment to validate the optional restart action.
# finacle-support-web
# finacle-support-web
