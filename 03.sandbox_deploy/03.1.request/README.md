# 요청 접수 API 와 요청 형식

Cloud Run 이 승인된 요청 JSON 을 받아 검증하고 Cloud Build 를 시작합니다.

| 파일 | 역할 |
|---|---|
| `sandbox-request.schema.json` | 요청 형식 (schema 2.0) |
| `main.py` | 형식 · 환경 규칙 · 이전 적용분과의 비교, 번들 생성, 빌드 시작 |
| `provisioner-env.yaml` | 환경마다 다른 값 (프로젝트 · 도메인 · 영역 · 이름 규칙) |
| `examples/` | 신규 · 이전 · 다중 컨테이너 예시 |

## 요청은 과제의 전체 상태

포털은 과제의 **현재 원하는 모습 전체**를 보냅니다. 바꿀 값만 고쳐 다시 보내면 됩니다.

| 하려는 일 | 바꾸는 값 |
|---|---|
| 신규 과제 | `action: CREATE`, `task.origin: NEW` |
| 기존 VM 과제 이전 | `action: CREATE`, `task.origin: MIGRATED`, `storage.existing_disk` |
| 중지 / 기동 | `workload.running` |
| 증설 | `workload.cpu`, `memory_gib`, `storage.size_gb` |
| 이미지 교체 | `workload.containers[].image` (digest) |
| 컨테이너 추가 | `workload.containers[]` 에 항목 추가 (포트 · 경로 다르게) |

`UPDATE` 는 매번 새 `request_id` 로 보냅니다.

## 주요 항목

| 경로 | 내용 |
|---|---|
| `identity.manage_group` · `project.manage` · `data.manage` | `true` 인 단계만 실행. 이전 과제는 모두 `false` |
| `identity.jupyter_gsa` | Pod 이 쓰는 과제 프로젝트의 서비스 계정. 이전 과제는 VM 이 쓰던 계정, `create: false` |
| `workload.area` · `namespace` | 영역과 Namespace. 규칙은 `ALLOWED_AREAS`, `NAMESPACE_TEMPLATE` |
| `workload.storage` | `size_gb`, `zone`, 이전 과제는 `existing_disk` |
| `workload.containers[]` | `image`(digest 고정), `hub_port`(8080~8089), `host`, `base_path` |
| `workload.hub_config` | `MANAGED`(템플릿 마운트) / `IMAGE`(이미지 설정 그대로) |

`base_path` 가 `/` 면 호스트 방식(`https://<host>/`), 그 외는 경로 방식(`https://<host>/general/rsch/`)입니다.

## 바꿀 수 없는 값

과제 프로젝트, 그룹, 서비스 계정, 영역, Namespace, 영역(zone), 기존 디스크 이름은 처음 적용한 뒤 바꿀 수 없고, 디스크는 줄일 수 없습니다. Runner 가 성공할 때마다 `gs://<BUNDLE_BUCKET>/tasks/<과제>/applied-request.json` 에 기록하고, 다음 요청을 이것과 비교합니다.
