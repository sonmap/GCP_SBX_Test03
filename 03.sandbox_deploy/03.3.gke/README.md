# Runner — ALB 연결

Terraform 은 모두 Infrastructure Manager 가 실행합니다. 이 이미지는 그 뒤의 **ALB 연결 단계**만 맡습니다.

| 파일 | 하는 일 |
|---|---|
| `reconcile_postdeploy.py` | 요청 JSON 내려받기 → 클러스터 접속 → Workload Identity 확인 → 컨테이너별 백엔드 서비스 + NEG 연결 → URL Map 경로 규칙 추가 → 상태 확인 → 적용 기록 |
| `Dockerfile` | gcloud · kubectl · Python 만 담은 이미지 |
| `cloudbuild/` | 릴리스 게시 (이미지 빌드, Terraform ZIP 게시) |

**URL Map** · 모든 과제가 하나를 같이 쓰므로 Terraform 에 넣지 않았습니다. 내보내기 → 규칙 추가 → 가져오기 순서이며, 그 사이 다른 과제가 바꾸면 fingerprint 불일치로 실패하고 다시 시도합니다.
