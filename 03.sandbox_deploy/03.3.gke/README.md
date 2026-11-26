# Runner — 워크로드와 ALB

Private Pool 의 Cloud Build 가 이 이미지로 두 스크립트를 차례로 실행합니다.

| 파일 | 하는 일 |
|---|---|
| `run_workload.py` | 요청 JSON → tfvars → `terraform plan` → 삭제 검사 → `apply` ([50-workload](../03.2.terraform/deployments/50-workload)) |
| `reconcile_postdeploy.py` | 컨테이너별 백엔드 서비스 + NEG 연결, URL Map 규칙 추가, 상태 확인, 적용 기록 |
| `Dockerfile` | gcloud · kubectl · Terraform · Provider 를 담은 Runner 이미지 |
| `cloudbuild/` | 릴리스 게시 (이미지 빌드, Terraform ZIP 게시) |

**삭제 검사** · plan 이 Namespace · PV · PVC · Service · KSA · Workload Identity 바인딩을 지우거나 교체하려 하면 적용하지 않고 멈춥니다.

**OAuth 값** · Secret Manager 에서 읽어 `kubectl` 로 과제 Namespace 에 넣습니다. Terraform State 에는 들어가지 않습니다.

**URL Map** · 모든 과제가 하나를 같이 씁니다. 내보내기 → 규칙 추가 → 가져오기 순서이며, 그 사이 다른 과제가 바꾸면 fingerprint 불일치로 실패하고 다시 시도합니다.
