#!/usr/bin/env bash
# 2nd deployment in one command: build the request (2-2), check it, publish it and
# start the Workflow (3). The request number is chosen automatically.
#
# Usage (infra-son01, repository root):
#   ./03.sandbox_deploy/run-request.sh test-task01-general.json
#   ./03.sandbox_deploy/run-request.sh test-task11-sa.json
#
# Optional environment overrides: JUPYTER_IMAGE_NAME, JUPYTER_IMAGE_TAG, BILLING_ACCOUNT.
set -euo pipefail

EXAMPLE="${1:?usage: $0 <example file in 03.sandbox_deploy/03.1.request/examples/>}"
SBX_GKE_PROJECT="gcp-sbx-gke01"
REGION="asia-northeast3"
ADMIN_ACCOUNT="user01@sonmap.net"
TF_MANAGER="tf-manager@gcp-sbx-gke01.iam.gserviceaccount.com"
BUNDLE_BUCKET="gcp-sbx-gke01-sbxtest01-bundles"
REQUEST_BUCKET="gcp-sbx-gke01-sbxtest01-requests"
AR_REPO="asia-northeast3-docker.pkg.dev/gcp-prod-edp-edge-509423/ar-sbxtest01-platform"
JUPYTER_IMAGE_NAME="${JUPYTER_IMAGE_NAME:-sbxtest01-jupyter-test}"
JUPYTER_IMAGE_TAG="${JUPYTER_IMAGE_TAG:-v1}"
BILLING_ACCOUNT="${BILLING_ACCOUNT:-019BE7-53DD39-AF5098}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${ROOT}/03.sandbox_deploy/03.1.request/examples/${EXAMPLE}"
[ -f "${SRC}" ] || { echo "예시 파일 없음: ${SRC}"; exit 1; }
cd "${ROOT}"
mkdir -p .local

TASK="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["task"]["name"])' "${SRC}")"
PREFIX="REQ-TEST-$(echo "${TASK}" | tr '[:lower:]' '[:upper:]')"

echo "== 1/5 최신 릴리스"
RELEASE_ID="$(gcloud storage ls -l "gs://${BUNDLE_BUCKET}/platform-releases/*/release.json" --account="${ADMIN_ACCOUNT}" \
  | grep release.json | sort -k2 | tail -n1 | sed -E 's#.*/platform-releases/([^/]+)/release.json#\1#')"
[ -n "${RELEASE_ID}" ] || { echo "게시된 릴리스가 없습니다 → README 1번"; exit 1; }
gcloud storage cat "gs://${BUNDLE_BUCKET}/platform-releases/${RELEASE_ID}/release.json" \
  --account="${ADMIN_ACCOUNT}" > .local/release.json
echo "RELEASE_ID=${RELEASE_ID}"

echo "== 2/5 이미지 digest"
JUPYTER_DIGEST="$(gcloud artifacts docker images describe "${AR_REPO}/${JUPYTER_IMAGE_NAME}:${JUPYTER_IMAGE_TAG}" \
  --account="${ADMIN_ACCOUNT}" --impersonate-service-account="${TF_MANAGER}" \
  --format='value(image_summary.digest)' 2>/dev/null || true)"
[[ "${JUPYTER_DIGEST}" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "이미지 ${JUPYTER_IMAGE_NAME}:${JUPYTER_IMAGE_TAG} 없음 → README 2-0"; exit 1; }
echo "JUPYTER_DIGEST=${JUPYTER_DIGEST}"

echo "== 3/5 다음 요청 번호"
LAST="$(gcloud storage ls "gs://${REQUEST_BUCKET}/approved/" --account="${ADMIN_ACCOUNT}" 2>/dev/null \
  | sed -nE "s#.*/approved/${PREFIX}-([0-9]{3})/#\1#p" | sort -n | tail -n1)"
REQUEST_ID="${PREFIX}-$(printf '%03d' $((10#${LAST:-0} + 1)))"
echo "REQUEST_ID=${REQUEST_ID}"

echo "== 4/5 요청 파일 · 점검"
OUT=".local/${REQUEST_ID}.json"
python3 - "${SRC}" .local/release.json "${REQUEST_ID}" "${AR_REPO}/${JUPYTER_IMAGE_NAME}@${JUPYTER_DIGEST}" "${OUT}" "${BILLING_ACCOUNT}" <<'PY'
import json, re, sys
src, rel, req_id, image, out, billing = sys.argv[1:]
request = json.load(open(src))
release = json.load(open(rel))
request["request_id"] = req_id
request["workload"]["containers"][0]["image"] = image
request["project"]["billing_account"] = billing
request["source"] = {k: release[k] for k in ("release_id", "archive_uri", "generation", "sha256")}
assert re.fullmatch(r"[a-z0-9./_:-]+@sha256:[0-9a-f]{64}", image), image
assert not billing.startswith("000000"), billing
json.dump(request, open(out, "w"), indent=2, ensure_ascii=False)
for key in ("request_id",):
    print(f"{key:16}: {request[key]}")
print(f"{'image':16}: {image}")
print(f"{'billing_account':16}: {billing}")
print(f"{'release_id':16}: {request['source']['release_id']}")
PY

echo "== 5/5 게시 · Workflow 실행"
REQUEST_URI="gs://${REQUEST_BUCKET}/approved/${REQUEST_ID}/request.json"
gcloud storage cp --if-generation-match=0 "${OUT}" "${REQUEST_URI}" --account="${ADMIN_ACCOUNT}"
GENERATION="$(gcloud storage objects describe "${REQUEST_URI}" --account="${ADMIN_ACCOUNT}" --format='value(generation)')"
SHA256="$(sha256sum "${OUT}" | cut -d' ' -f1)"
gcloud workflows run workflow-sbxtest01-provision \
  --project="${SBX_GKE_PROJECT}" --location="${REGION}" --account="${ADMIN_ACCOUNT}" \
  --data="{\"request_id\":\"${REQUEST_ID}\",\"approved_json_uri\":\"${REQUEST_URI}\",\"generation\":${GENERATION},\"sha256\":\"${SHA256}\"}" \
  --format="value(state)"
echo "→ SUCCEEDED 면 접수 완료. 진행 확인: README 4번 (TASK_NAME=${TASK})"
