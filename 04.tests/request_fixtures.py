"""Synthetic request data (schema 2.0) for local tests."""

DIGEST = "sha256:" + "ab" * 32
REGISTRY = "asia-northeast3-docker.pkg.dev/gcp-prod-edp-edge-509423/ar-sbxtest01-platform"
ENV = {
    "SBX_GKE_PROJECT": "gcp-sbx-gke01", "GCP_REGION": "asia-northeast3",
    "GKE_CLUSTER_NAME": "gke-sbx-main-an3",
    "BUNDLE_BUCKET": "gcp-sbx-gke01-sbxtest01-bundles",
    "REQUEST_BUCKET": "gcp-sbx-gke01-sbxtest01-requests",
    "TF_STATE_BUCKET": "gcp-sbx-gke01-sbxtest01-tfstate",
    "WORKER_POOL": "projects/gcp-sbx-gke01/locations/asia-northeast3/workerPools/pool-sbxtest01",
    "RUNNER_IMAGE_REPO": REGISTRY + "/sbxtest01-automation-runner",
    "DEPLOYMENT_PREFIX": "sbxtest01",
    "SHARED_URL_MAP_NAME": "urlmap-alb-sbxtest01-shared",
    "SHARED_HEALTH_CHECK_NAME": "hc-sbxtest01-jupyter-tcp",
    "CLOUD_IDENTITY_CUSTOMER_ID": "C123test",
    "GROUP_EMAIL_TEMPLATE": "pgrp-gcp-dev-{task}@sonmap.net",
    "MEMBER_EMAIL_DOMAINS": "sonmap.net",
    "NAMESPACE_TEMPLATE": "sbx-{area}-{task}",
    "ALLOWED_AREAS": "general,sa,com",
    "JUPYTER_HOST_SUFFIXES": ".sonmap.net",
    "IMAGE_REGISTRY_PREFIXES": "asia-northeast3-docker.pkg.dev/gcp-prod-edp-edge-509423/",
    "DATALAKE_PROJECTS": "pjt-c-admin",
}


def make_request(*, migrated=False, containers=1):
    """NEW: group, project, data and workload. MIGRATED: workload only."""
    task = "rsch" if migrated else "sbx03"
    project = f"gcp-sbx-edp-{task}"
    area = "general"
    request = {
        "schema_version": "2.0",
        "request_id": "REQ-LOCAL-TEST-001",
        "approval": {"status": "APPROVED", "approval_id": "test-approval", "approved_at": "2026-01-01T00:00:00Z"},
        "action": "CREATE",
        "task": {"name": task, "display_name": "Local test assignment", "expires_on": "2026-12-31",
                 "origin": "MIGRATED" if migrated else "NEW"},
        "identity": {
            "group_email": f"pgrp-gcp-dev-{task}@sonmap.net",
            "manage_group": not migrated,
            "jupyter_gsa": {
                "email": f"sa-sbx-{task}-notebook-1-user@{project}.iam.gserviceaccount.com",
                "create": not migrated,
            },
        },
        "project": {"manage": not migrated, "project_id": project},
        "data": {"manage": not migrated},
        "gke": {"project_id": "gcp-sbx-gke01", "cluster_name": "gke-sbx-main-an3", "location": "asia-northeast3"},
        "workload": {
            "area": area, "namespace": f"sbx-{area}-{task}", "running": True,
            "cpu": 8, "memory_gib": 64,
            "storage": {"size_gb": 100, "zone": "asia-northeast3-a"},
            "containers": [
                {"name": "jupyter" if containers == 1 else f"jupyter-{i + 1}",
                 "image": f"{REGISTRY}/{task}-jupyter@{DIGEST}", "hub_port": 8081 + i,
                 "host": "sbx.sonmap.net",
                 "base_path": f"/{area}/{task}/" if containers == 1 else f"/{area}/{task}/v{i + 1}/"}
                for i in range(containers)
            ],
        },
        "source": {
            "release_id": "git-test123",
            "archive_uri": "gs://gcp-sbx-gke01-sbxtest01-bundles/platform-releases/git-test123/sandbox-source.zip",
            "generation": 1, "sha256": "a" * 64,
        },
    }
    if migrated:
        request["workload"]["storage"]["existing_disk"] = f"disk-sbx-{task}-data"
    else:
        request["identity"]["members"] = ["tester01@sonmap.net", "tester02@sonmap.net"]
        request["project"].update(create_project=True, project_name="Local test sandbox",
                                  org_id="123456789012", billing_account="000000-000000-000000")
        request["data"].update(bigquery_dataset=f"{task}_main", gcs_bucket=f"{project}-data",
                               datalake_datasets=[{"project_id": "pjt-c-admin", "dataset_id": "dlk_sample"}])
    return request
