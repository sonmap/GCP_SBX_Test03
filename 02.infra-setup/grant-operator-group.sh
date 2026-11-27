#!/usr/bin/env bash
# 작업 그룹에 프로젝트 역할을 부여합니다.
#
# 역할은 콘솔에 보이는 "이름(title)"으로 적고, 실행할 때 gcloud 로 역할 ID 를 찾습니다.
# 이름이 0개 또는 2개 이상 일치하면 그 역할은 부여하지 않고 목록에 표시합니다.
# 기본은 계획만 출력합니다. --apply 를 붙여야 실제로 부여합니다.
#
# 수행 계정: 대상 프로젝트의 IAM 관리자 (Owner 또는 Project IAM Admin)
# 사용법:
#   ./02.infra-setup/grant-operator-group.sh            # 계획만
#   ./02.infra-setup/grant-operator-group.sh --apply    # 부여
set -euo pipefail

PROJECT="${SBX_GKE_PROJECT:-gcp-sbx-gke01}"
MEMBER="${OPERATOR_MEMBER:-group:pgrp-gcp-sbx01@sonmap.net}"
ACCOUNT="${IAM_ADMIN_ACCOUNT:-admin@sonmap.net}"
APPLY="${1:-}"

# 운영 환경 l2comn 관리 그룹과 같은 역할 (콘솔 표시 이름)
ROLE_TITLES=(
  "Artifact Registry Administrator"
  "BigQuery Admin"
  "CA Service Certificate Manager"
  "CA Service Certificate Requester"
  "CA Service Operation Manager"
  "Certificate Manager Owner"
  "Cloud Admin"
  "Cloud Asset Viewer"
  "Cloud Build Admin"
  "Cloud Memorystore Redis Admin"
  "Cloud Run Admin"
  "Cloud SQL Admin"
  "Compute Instance Admin (v1)"
  "Compute Load Balancer Admin"
  "Compute Network Admin"
  "IAP-secured Tunnel User"
  "Kubernetes Engine Admin"
  "Logging Admin"
  "Memorystore Admin"
  "Monitoring Editor"
  "Network Security Admin"
  "Project IAM Admin"
  "Secure Source Manager Admin"
  "Secure Source Manager Instance Accessor"
  "Secure Source Manager Instance Owner"
  "Secure Source Manager Instance Repository Creator"
  "Secure Source Manager Repository Reader"
  "Secure Source Manager Viewer"
  "Service Account Admin"
  "Service Account User"
  "Service Usage Admin"
  "Service Usage Consumer"
  "Storage Admin"
  "Tech Support Editor"
  "Workflows Admin"
)

catalog="$(mktemp)"; trap 'rm -f "$catalog"' EXIT
gcloud iam roles list --account="$ACCOUNT" --format='csv[no-heading,separator="|"](name,title)' > "$catalog"

resolved=(); missing=()
for title in "${ROLE_TITLES[@]}"; do
  ids="$(awk -F'|' -v t="$title" '$2==t {print $1}' "$catalog")"
  count="$(printf '%s' "$ids" | grep -c . || true)"
  if [[ "$count" == 1 ]]; then
    resolved+=("$ids"); printf '  %-52s %s\n' "$title" "$ids"
  else
    missing+=("$title ($count)"); printf '  %-52s %s\n' "$title" "-- 찾지 못함 ($count 건)"
  fi
done

echo
echo "대상 ${PROJECT} · 주체 ${MEMBER} · 부여 ${#resolved[@]} 건 · 제외 ${#missing[@]} 건"
if [[ "$APPLY" != "--apply" ]]; then
  echo "계획만 출력했습니다. 부여하려면 --apply 를 붙여 다시 실행합니다."
  exit 0
fi

for role in "${resolved[@]}"; do
  gcloud projects add-iam-policy-binding "$PROJECT" --account="$ACCOUNT" \
    --member="$MEMBER" --role="$role" --condition=None --quiet --format=none
  echo "[OK] $role"
done

echo
echo "현재 ${MEMBER} 의 역할:"
gcloud projects get-iam-policy "$PROJECT" --account="$ACCOUNT" --flatten=bindings \
  --filter="bindings.members:${MEMBER}" --format='value(bindings.role)' | sort
