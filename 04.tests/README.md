# 로컬 검증

GCP 접속 없이 코드와 설정이 서로 맞는지 확인합니다. 실제 배포 · 로그인 · 디스크 보존은 실환경에서 확인합니다.

| 파일 | 확인하는 내용 |
|---|---|
| `test_provisioning.py` | 요청 형식, 환경 규칙, 변경 불가 값, 단계 선택, 빌드 구성 |
| `test_workload.py` | 요청 → tfvars 대응, 삭제 차단, `50-workload` 의 디스크 보호 설정 |
| `test_internal_alb.py` | URL Map 규칙 병합(경로 · 호스트 방식), 백엔드 서비스, Workload Identity 확인 |
| `test_build_paths.py` | Cloud Build 의 Docker 경로 |
| `test_release_bootstrap.py` | 릴리스 게시 설정 |
| `test_commandbooks.py` | 인프라 명령서와 설정 파일의 일치 |

```bash
python3 -m venv .local/test-venv
.local/test-venv/bin/python -m pip install -r 04.tests/requirements-dev.txt
.local/test-venv/bin/python -m unittest discover -s 04.tests -v
```

`test_commandbooks.py` 의 두 항목은 건너뜁니다. Test02 에서 이미 명령서와 어긋나 있던 검사이며, 명령서 블록을 실제로 실행하는 방식이라 정리 대상입니다.
