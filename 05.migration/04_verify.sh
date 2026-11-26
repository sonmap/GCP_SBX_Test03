#!/usr/bin/env bash
# Read-only check of one migrated task.
# Usage: ./04_verify.sh NAMESPACE TASK EXPECTED_GSA URL
set -uo pipefail
ns="${1:?NAMESPACE}"; task="${2:?TASK}"; gsa="${3:?EXPECTED_GSA}"; url="${4:?URL}"
fail=0
check() { if eval "$2"; then echo "[OK]   $1"; else echo "[FAIL] $1"; fail=1; fi; }

check "PVC bound"            '[[ "$(kubectl -n "$ns" get pvc data -o jsonpath={.status.phase})" == Bound ]]'
check "PV keeps the disk"    '[[ "$(kubectl get pv "pv-${ns}-data" -o jsonpath={.spec.persistentVolumeReclaimPolicy})" == Retain ]]'
check "Pod ready"            '[[ "$(kubectl -n "$ns" get deploy jupyter -o jsonpath={.status.readyReplicas})" == 1 ]]'
check "Workload Identity"    '[[ "$(kubectl -n "$ns" exec deploy/jupyter -- gcloud auth list --format="value(account)" 2>/dev/null | head -1)" == "$gsa" ]]'
check "home directories"     '[[ -n "$(kubectl -n "$ns" exec deploy/jupyter -- ls /home/jupyter 2>/dev/null)" ]]'
check "login page via ALB"   '[[ "$(curl -sk -o /dev/null -w "%{http_code}" "${url}hub/login")" =~ ^(200|302)$ ]]'
exit $fail
