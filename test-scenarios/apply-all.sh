#!/usr/bin/env bash
# ============================================================
# apply-all.sh — Apply all kas test scenarios to your cluster
#
# Usage:
#   bash test-scenarios/apply-all.sh          # apply all
#   bash test-scenarios/apply-all.sh --delete  # tear everything down
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAMESPACE="kas-test"

# ── Colour helpers ───────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}[INFO]${RESET}  $*"; }
ok()      { echo -e "${GREEN}[OK]${RESET}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
section() { echo -e "\n${BOLD}${CYAN}=== $* ===${RESET}"; }

# ── Ordered list of scenario files ──────────────────────────
SCENARIOS=(
  "00-namespace.yaml"
  "01-crashloop.yaml"
  "02-oomkilled.yaml"
  "03-imagepullbackoff.yaml"
  "04-pending-unschedulable.yaml"
  "05-deployment-failing.yaml"
  "06-liveness-probe-fail.yaml"
  "07-missing-secret.yaml"
  "08-service-no-endpoints.yaml"
)

# ── Delete mode ─────────────────────────────────────────────
if [[ "${1:-}" == "--delete" ]]; then
  section "Tearing down all kas-test scenarios"
  for f in "${SCENARIOS[@]}"; do
    file="$SCRIPT_DIR/$f"
    if [[ -f "$file" ]]; then
      info "Deleting resources from $f"
      kubectl delete -f "$file" --ignore-not-found=true || true
    fi
  done
  info "Deleting namespace $NAMESPACE (this removes everything remaining)"
  kubectl delete namespace "$NAMESPACE" --ignore-not-found=true || true
  ok "All test scenarios removed."
  exit 0
fi

# ── Apply mode ──────────────────────────────────────────────
section "Applying kas test scenarios to namespace: $NAMESPACE"

# Check kubectl is available
if ! command -v kubectl &>/dev/null; then
  echo -e "${RED}[ERROR]${RESET} kubectl not found in PATH. Install it first." >&2
  exit 1
fi

# Check cluster is reachable
if ! kubectl cluster-info &>/dev/null 2>&1; then
  echo -e "${RED}[ERROR]${RESET} Cannot reach Kubernetes cluster. Check kubeconfig / context." >&2
  exit 1
fi

ok "Cluster reachable: $(kubectl config current-context)"

# Apply in order
for f in "${SCENARIOS[@]}"; do
  file="$SCRIPT_DIR/$f"
  if [[ ! -f "$file" ]]; then
    warn "Skipping $f — file not found"
    continue
  fi
  info "Applying $f"
  kubectl apply -f "$file"
done

ok "All scenarios applied."

# ── Brief status check ───────────────────────────────────────
section "Waiting 15 s for pods to initialise..."
sleep 15

section "Pod status in $NAMESPACE"
kubectl get pods -n "$NAMESPACE" -o wide

section "Service / PVC status in $NAMESPACE"
kubectl get svc,pvc -n "$NAMESPACE"

echo ""
echo -e "${BOLD}${GREEN}Ready to test!${RESET} Run kas commands against the failing resources:"
echo ""
echo -e "  ${CYAN}kas \"why is crash-loop-pod crashing?\" -n kas-test${RESET}"
echo -e "  ${CYAN}kas \"oom-pod keeps restarting\" -n kas-test${RESET}"
echo -e "  ${CYAN}kas \"imagepull-wrong-name failing\" -n kas-test${RESET}"
echo -e "  ${CYAN}kas \"pending-pvc-not-bound pod is stuck\" -n kas-test${RESET}"
echo -e "  ${CYAN}kas \"failing-deployment not ready\" -n kas-test${RESET}"
echo -e "  ${CYAN}kas \"liveness-probe-pod keeps restarting\" -n kas-test${RESET}"
echo -e "  ${CYAN}kas \"missing-secret-pod not starting\" -n kas-test${RESET}"
echo -e "  ${CYAN}kas \"no-endpoint-svc has no backends\" -n kas-test${RESET}"
echo ""
echo -e "To clean up: ${YELLOW}bash test-scenarios/apply-all.sh --delete${RESET}"
