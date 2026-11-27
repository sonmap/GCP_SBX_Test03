# 과제별 Terraform

코드는 한 벌, State 는 **과제 × 단계마다 하나**이고 모두 **같은 버킷 · 같은 규칙**으로 저장됩니다. 실행도 한 곳(Private Pool 의 Runner, Terraform CLI)에서만 합니다.

```
gs://gcp-sbx-gke01-sbxtest01-tfstate/tasks/<과제>/05-group/default.tfstate
                                              /10-project/default.tfstate
                                              /30-project-iam/default.tfstate
                                              /40-data/default.tfstate
                                              /50-workload/default.tfstate
```

| 단계 | 만드는 것 | 실행 조건 |
|---|---|---|
| `05-group` | Cloud Identity 그룹 · 구성원 | `identity.manage_group` |
| `10-project` | 프로젝트 · API · 자동화 IAM | `project.manage` |
| `30-project-iam` | 그룹의 프로젝트 권한 | `project.manage` |
| `40-data` | BigQuery · GCS · 서비스 계정 · 데이터 권한 | `data.manage` |
| `50-workload` | Namespace · KSA · Workload Identity · PV/PVC · Pod · Service | 항상 |

모든 단계가 같은 순서로 실행됩니다: `init`(과제·단계 State) → `plan` → 삭제 검사 → `apply`. 기존 VM 과제(`origin: MIGRATED`)는 `50-workload` State 하나만 생깁니다.

**State 조회**

```bash
gcloud storage ls -r gs://gcp-sbx-gke01-sbxtest01-tfstate/tasks/<과제>/
```

**삭제 검사** · plan 이 아래 자원을 지우거나 교체하려 하면 그 단계에서 멈추고 적용하지 않습니다. 구성원 제외와 IAM 권한 회수는 허용합니다.

| 단계 | 보호 자원 |
|---|---|
| 05-40 | 그룹 · 프로젝트 · 프로젝트 API · 서비스 계정 · BigQuery 데이터셋 · GCS 버킷 |
| 50 | Namespace · PV · PVC · Service · KSA · Workload Identity 바인딩 |

## 50-workload

```mermaid
flowchart TB
    subgraph NS["Namespace · 과제 1개"]
      KSA["KSA"] -.Workload Identity.-> GSA["과제 프로젝트의 서비스 계정"]
      D["Deployment · Recreate<br/>replicas = running ? 1 : 0"] --> POD["Pod<br/>컨테이너 1개 (필요 시 여러 개)"]
      POD --> PVC["PVC data → /home/jupyter"]
      SVC["Service · 포트별 NEG"] --> POD
    end
    PVC --> PV["기존 디스크: 정적 PV (Retain)<br/>신규: 동적 생성"]
```

| 요청 값 | Terraform 에서 바뀌는 것 |
|---|---|
| `running` | `replicas` 1 ↔ 0. 디스크 · 경로는 유지 |
| `cpu` · `memory_gib` | Pod 자원. Pod 재시작 |
| `storage.size_gb` (증가만) | PVC 크기 |
| `containers[].image` | 이미지 교체. Pod 재시작 |
| `containers[]` 추가 | 같은 Pod 에 컨테이너 · 포트 추가 |

**지워지지 않게 한 것** · PV 와 PVC 는 `prevent_destroy`, PV 는 `Retain` 입니다. Runner 도 적용 전에 삭제 계획을 막습니다.

**Hub 설정** · `hub_config: MANAGED` 면 [`templates/jupyterhub_config.py.tftpl`](deployments/50-workload/templates/jupyterhub_config.py.tftpl) 을 이미지의 설정 파일 위에 덮어 마운트합니다. VM 의 설정과 다른 점은 세 가지입니다 — 접속 주소가 ALB 주소, OAuth 값은 파일에서 읽음, 내부 포트가 컨테이너마다 다름. **이 템플릿은 참고본이므로 실제 이미지 안의 파일과 비교한 뒤 사용합니다.** `IMAGE` 면 이미지 안의 설정을 그대로 씁니다.
