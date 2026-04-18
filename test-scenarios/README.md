# kas Test Scenarios

Eight intentionally broken Kubernetes resources to test every major failure mode that `kas` diagnoses.

## Quick start

```bash
# Apply all scenarios (requires a running cluster)
bash test-scenarios/apply-all.sh

# Tear everything down when done
bash test-scenarios/apply-all.sh --delete
```

Or apply a single scenario:

```bash
kubectl apply -f test-scenarios/01-crashloop.yaml
```

All resources live in the **`kas-test`** namespace (created by `00-namespace.yaml`).

---

## Scenarios

### 00 — Namespace

`00-namespace.yaml` — Creates the `kas-test` namespace with scenario labels. Must be applied first.

---

### 01 — CrashLoopBackOff

`01-crashloop.yaml` — Pod exits with code 1 every time, simulating a missing config file.

**What you see:** `CrashLoopBackOff`, restart count climbing, exponential back-off delay.

```bash
kas "why is crash-loop-pod crashing?" -n kas-test
kas "pod crashloop" -n kas-test -r crash-loop-pod -t pod
```

---

### 02 — OOMKilled

`02-oomkilled.yaml` — Container requests 100 MB of memory but is limited to 30 Mi. Linux OOM killer terminates it with exit code 137.

**What you see:** `OOMKilled` in `lastState`, restart count increasing, `reason: OOMKilled`.

```bash
kas "oom-pod keeps restarting" -n kas-test
kas "why is oom-pod killed?" -n kas-test -r oom-pod -t pod
```

---

### 03 — ImagePullBackOff

`03-imagepullbackoff.yaml` — Two pods:

| Pod | Image | Reason |
|-----|-------|--------|
| `imagepull-wrong-name` | `this-registry-does-not-exist.io/my-app:v1.0.0` | Registry unreachable |
| `imagepull-wrong-tag` | `nginx:v99.99.99-does-not-exist` | Tag not found |

**What you see:** `ErrImagePull` → `ImagePullBackOff`, back-off timer in events.

```bash
kas "imagepull-wrong-name failing to start" -n kas-test
kas "why cant kubernetes pull the image?" -n kas-test
```

---

### 04 — Pending / PVC Not Bound

`04-pending-unschedulable.yaml` — A PVC referencing a StorageClass that does not exist stays Pending indefinitely. The pod mounting it also stays Pending — Kubernetes cannot schedule it until the volume is ready.

| Resource | Reason |
|----------|--------|
| `stuck-pvc` (PVC) | StorageClass `ultra-fast-nvme-does-not-exist` — doesn't exist |
| `pending-pvc-not-bound` (Pod) | Waits for the stuck PVC above |

```bash
kas "pending-pvc-not-bound pod is stuck" -n kas-test
kas "why is stuck-pvc not binding?" -n kas-test -r stuck-pvc -t pvc
```

---

### 05 — Deployment Rollout Stuck

`05-deployment-failing.yaml` — A 3-replica Deployment where every pod crashes with exit code 2 (fake database connection failure). Rollout never completes; service has zero endpoints.

**What you see:** `READY 0/3`, rollout stuck, events piling up, service endpoints empty.

```bash
kas "failing-deployment not ready" -n kas-test
kas "why is failing-deployment rollout stuck?" -n kas-test -r failing-deployment -t deployment
kas "failing-deployment-svc has no endpoints" -n kas-test
```

---

### 06 — Liveness Probe Failing

`06-liveness-probe-fail.yaml` — Container runs a tiny HTTP server that always returns `500` on `/healthz`. Kubernetes kills and restarts the pod every ~15 s.

**What you see:** Pod shows `Running` briefly then restarts; restart count climbs; events show `Liveness probe failed`.

```bash
kas "liveness-probe-pod keeps restarting" -n kas-test
kas "why is liveness-probe-pod being killed?" -n kas-test -r liveness-probe-pod -t pod
```

---

### 07 — Missing Secret / ConfigMap

`07-missing-secret.yaml` — Two pods that can never start because their referenced Secrets / ConfigMaps don't exist:

| Pod | Missing resource | Error |
|-----|-----------------|-------|
| `missing-secret-pod` | Secret `db-credentials` + `external-api-secret` | `CreateContainerConfigError` |
| `missing-configmap-pod` | ConfigMap `app-settings` + `app-config` | `CreateContainerConfigError` |

```bash
kas "missing-secret-pod not starting" -n kas-test
kas "why is missing-secret-pod in error?" -n kas-test -r missing-secret-pod -t pod
kas "missing-configmap-pod stuck" -n kas-test
```

---

### 08 — Service with No Endpoints

`08-service-no-endpoints.yaml` — Two Service misconfiguration variants:

| Service | Problem | Effect |
|---------|---------|--------|
| `no-endpoint-svc` | Selector `app=wrong-backend` (pod has `app=real-backend`) | 0 endpoints — traffic dropped |
| `wrong-port-svc` | `targetPort: 9090` (pod listens on `8080`) | Connection refused |

```bash
kas "no-endpoint-svc has no backends" -n kas-test
kas "why is no-endpoint-svc not routing traffic?" -n kas-test -r no-endpoint-svc -t service
kas "wrong-port-svc connection refused" -n kas-test
```

---

## Useful kubectl checks while testing

```bash
# Watch pod status live
kubectl get pods -n kas-test -w

# Check events for a specific pod
kubectl get events -n kas-test --field-selector involvedObject.name=crash-loop-pod

# Inspect service endpoints
kubectl get endpoints -n kas-test

# Check PVC binding status
kubectl get pvc -n kas-test

# Check deployment rollout
kubectl rollout status deployment/failing-deployment -n kas-test

# See restart counts at a glance
kubectl get pods -n kas-test -o custom-columns=\
'NAME:.metadata.name,STATUS:.status.phase,RESTARTS:.status.containerStatuses[0].restartCount,REASON:.status.containerStatuses[0].state.waiting.reason'
```

---

## Cleanup

```bash
# Remove everything
bash test-scenarios/apply-all.sh --delete

# Or just delete the namespace (removes all resources inside it)
kubectl delete namespace kas-test
```
