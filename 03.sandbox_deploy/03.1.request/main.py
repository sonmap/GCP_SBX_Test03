import hashlib
import hmac
import io
import json
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

from flask import Flask, jsonify, request
from google.api_core.client_options import ClientOptions
from google.api_core.exceptions import PreconditionFailed
from google.cloud import storage
from google.cloud.devtools import cloudbuild_v1
from jsonschema import Draft202012Validator, FormatChecker

app = Flask(__name__)
REQUEST_RE = re.compile(r"^REQ-[A-Z0-9-]{6,64}$")
SCHEMA = json.loads(Path(__file__).with_name("sandbox-request.schema.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())

# stage -> (archive name, Infrastructure Manager deployment suffix).
# Every stage is one Infrastructure Manager deployment per task, and
# Infrastructure Manager keeps its state:
#   projects/<gke project>/locations/<region>/deployments/<prefix>-<task>-<hash>-<suffix>
STAGES = {
    "05-group": ("group.zip", "group"),
    "10-project": ("project.zip", "project"),
    "30-project-iam": ("project-iam.zip", "project-iam"),
    "40-data": ("data.zip", "data"),
    "50-workload": ("workload.zip", "workload"),
}
# Terraform source layout inside the released sandbox-source.zip.
SOURCE_DEPLOYMENTS_DIR = "deployments"
# Fields that can never change after the first apply. A change means a new task.
IMMUTABLE_FIELDS = (
    ("task", "origin"),
    ("project", "project_id"),
    ("identity", "group_email"),
    ("identity", "jupyter_gsa", "email"),
    ("gke", "project_id"),
    ("gke", "cluster_name"),
    ("workload", "area"),
    ("workload", "namespace"),
    ("workload", "storage", "zone"),
    ("workload", "storage", "existing_disk"),
    ("workload", "storage", "storage_class"),
)


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


def hub_auth_mode() -> str:
    """oauth = real login server. dummy = no login check, for tests only."""
    mode = os.environ.get("HUB_AUTH_MODE", "oauth")
    if mode not in ("oauth", "dummy"):
        raise ValueError("HUB_AUTH_MODE must be oauth or dummy")
    return mode


def enabled_stages(payload: dict) -> list[str]:
    """Stages this request applies, in order. 50-workload always runs; the
    others only when the request manages them (migrated tasks manage none)."""
    stages = []
    if payload["identity"]["manage_group"]:
        stages.append("05-group")
    if payload["project"]["manage"]:
        stages.extend(["10-project", "30-project-iam"])
    if payload["data"]["manage"]:
        stages.append("40-data")
    stages.append("50-workload")
    return stages


def deployment_name(payload: dict, stage: str) -> str:
    prefix = os.environ.get("DEPLOYMENT_PREFIX", "sbx")
    key = hashlib.sha256(payload["project"]["project_id"].encode()).hexdigest()[:8]
    return f"{prefix}-{payload['task']['name']}-{key}-{STAGES[stage][1]}"


def workload_variables(payload: dict) -> dict:
    """Inputs of 50-workload. Environment values come from provisioner-env."""
    task = payload["task"]
    workload = payload["workload"]
    storage = workload["storage"]
    existing_disk = storage.get("existing_disk", "")
    default_class = os.environ.get("STATIC_STORAGE_CLASS", "sbx-static-pd") if existing_disk else os.environ.get(
        "DYNAMIC_STORAGE_CLASS", "standard-rwo")
    return {
        "gke_project_id": payload["gke"]["project_id"],
        "cluster_name": payload["gke"]["cluster_name"],
        "cluster_location": payload["gke"]["location"],
        "gke_endpoint_mode": os.environ.get("GKE_ENDPOINT_MODE", "dns"),
        "wi_impersonate_service_account": os.environ.get("WI_IMPERSONATE_SA", ""),
        "task_name": task["name"],
        "task_project_id": payload["project"]["project_id"],
        "area": workload["area"],
        "namespace": workload["namespace"],
        "origin": task["origin"],
        "request_id": payload["request_id"],
        "jupyter_gsa_email": payload["identity"]["jupyter_gsa"]["email"],
        "ksa_name": f"ksa-jupyter-{task['name']}",
        "running": workload["running"],
        "cpu": workload["cpu"],
        "memory_gib": workload["memory_gib"],
        "ephemeral_storage_gib": workload.get("ephemeral_storage_gib", 10),
        "compute_class": workload.get("compute_class", "Balanced"),
        "gpu": workload.get("gpu"),
        "disk_size_gb": storage["size_gb"],
        "zone": storage["zone"],
        "existing_disk": existing_disk,
        "storage_class": storage.get("storage_class", default_class),
        "containers": [
            {key: item[key] for key in ("name", "image", "hub_port", "host", "base_path", "command", "args")
             if key in item}
            for item in workload["containers"]
        ],
        "hub_config_managed": workload.get("hub_config", "MANAGED") == "MANAGED",
        "hub_auth_mode": hub_auth_mode(),
        "hub_oauth_base_url": os.environ.get("HUB_OAUTH_BASE_URL", ""),
        "portal_members_api": os.environ.get("PORTAL_MEMBERS_API", ""),
        "hub_admin_users": env_list("HUB_ADMIN_USERS"),
    }


def parse_gs_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("gs://"):
        raise ValueError("approved_json_uri must use gs://")
    bucket, separator, name = uri[5:].partition("/")
    if not bucket or not separator or not name:
        raise ValueError("approved_json_uri must include bucket and object")
    return bucket, name


def download_approved_request(uri: str, generation: int, expected_sha256: str) -> tuple[dict, bytes]:
    bucket_name, object_name = parse_gs_uri(uri)
    if bucket_name != os.environ["REQUEST_BUCKET"]:
        raise ValueError("request bucket is not allowed")

    blob = storage.Client().bucket(bucket_name).blob(object_name, generation=generation)
    raw = blob.download_as_bytes()
    actual_hash = hashlib.sha256(raw).hexdigest()
    if not hmac.compare_digest(actual_hash, expected_sha256.lower()):
        raise ValueError("request sha256 mismatch")

    payload = json.loads(raw)
    errors = sorted(VALIDATOR.iter_errors(payload), key=lambda error: list(error.path))
    if errors:
        location = ".".join(str(value) for value in errors[0].path) or "$"
        raise ValueError(f"request schema validation failed at {location}: {errors[0].message}")
    validate_semantics(payload)
    return payload, raw


def validate_semantics(payload: dict) -> None:
    """Environment policy. Naming rules live in env vars, not in the schema."""
    identity, project, data = payload["identity"], payload["project"], payload["data"]
    gke, workload = payload["gke"], payload["workload"]
    task = payload["task"]["name"]
    migrated = payload["task"]["origin"] == "MIGRATED"

    if payload["source"]["sha256"] == "0" * 64:
        raise ValueError("publish a platform release and update request source first")

    # --- identity -----------------------------------------------------------
    expected_group = os.environ["GROUP_EMAIL_TEMPLATE"].format(task=task)
    if identity["group_email"] != expected_group:
        raise ValueError(f"group_email must be {expected_group}")
    members = identity.get("members", [])
    if identity["manage_group"]:
        customer_id = os.environ.get("CLOUD_IDENTITY_CUSTOMER_ID", "")
        if not re.fullmatch(r"C[A-Za-z0-9]+", customer_id):
            raise ValueError("configure CLOUD_IDENTITY_CUSTOMER_ID before deployment")
        if not members:
            raise ValueError("identity.members is required when manage_group=true")
        domains = env_list("MEMBER_EMAIL_DOMAINS")
        for member in members:
            if member.rsplit("@", 1)[1].lower() not in domains:
                raise ValueError(f"member domain is not allowed: {member}")
    group_name = identity.get("group_resource_name")
    membership_names = identity.get("membership_resource_names", {})
    if membership_names and not group_name:
        raise ValueError("group_resource_name is required when importing memberships")
    if not set(membership_names).issubset(members):
        raise ValueError("imported membership keys must be approved identity.members")
    for name in membership_names.values():
        if not name.startswith(f"{group_name}/memberships/"):
            raise ValueError("membership resource must belong to group_resource_name")

    gsa = identity["jupyter_gsa"]
    if not gsa["email"].endswith(f"@{project['project_id']}.iam.gserviceaccount.com"):
        raise ValueError("jupyter_gsa.email must be a service account of the task project")
    if gsa["create"] and not data["manage"]:
        raise ValueError("jupyter_gsa.create=true requires data.manage=true (40-data creates it)")
    if project.get("create_project", False) and not gsa["create"]:
        raise ValueError("new projects require identity.jupyter_gsa.create=true")
    if data.get("datalake_datasets"):
        if not data["manage"]:
            raise ValueError("datalake access requires data.manage=true")
        allowed = env_list("DATALAKE_PROJECTS")
        for item in data["datalake_datasets"]:
            if item["project_id"] not in allowed:
                raise ValueError(f"datalake project is not allowed: {item['project_id']}")

    # --- migrated tasks: never touch what already exists ------------------------
    if migrated:
        if project.get("create_project", False) or gsa["create"]:
            raise ValueError("MIGRATED tasks reuse the existing project and service account")
        if not workload["storage"].get("existing_disk"):
            raise ValueError("MIGRATED tasks require workload.storage.existing_disk")

    # --- GKE / workload -------------------------------------------------------
    if gke["project_id"] != os.environ["SBX_GKE_PROJECT"]:
        raise ValueError("gke.project_id must be the platform GKE project")
    if gke["cluster_name"] != os.environ["GKE_CLUSTER_NAME"]:
        raise ValueError("gke.cluster_name must be the platform cluster")
    if gke["location"] != os.environ["GCP_REGION"]:
        raise ValueError("gke.location must be the platform region")
    if workload["area"] not in env_list("ALLOWED_AREAS"):
        raise ValueError(f"workload.area must be one of {env_list('ALLOWED_AREAS')}")
    expected_namespace = os.environ["NAMESPACE_TEMPLATE"].format(task=task, area=workload["area"])
    if workload["namespace"] != expected_namespace:
        raise ValueError(f"workload.namespace must be {expected_namespace}")
    if not workload["storage"]["zone"].startswith(gke["location"] + "-"):
        raise ValueError("workload.storage.zone must be a zone of gke.location")

    # Autopilot bills what it rounds up to; keep requests inside a real ratio.
    ratio = workload["memory_gib"] / workload["cpu"]
    max_ratio = float(os.environ.get("MAX_MEMORY_PER_CPU", "8"))
    if ratio < 1 or ratio > max_ratio:
        raise ValueError(f"memory_gib per cpu must be between 1 and {max_ratio:g}")

    containers = workload["containers"]
    prefixes = env_list("IMAGE_REGISTRY_PREFIXES")
    host_suffixes = env_list("JUPYTER_HOST_SUFFIXES")
    for field in ("name", "hub_port"):
        values = [item[field] for item in containers]
        if len(values) != len(set(values)):
            raise ValueError(f"containers[].{field} must be unique within the task")
    routes = [(item["host"], item["base_path"]) for item in containers]
    if len(routes) != len(set(routes)):
        raise ValueError("containers[] host + base_path must be unique within the task")
    for item in containers:
        if not any(item["image"].startswith(prefix) for prefix in prefixes):
            raise ValueError(f"image registry is not allowed: {item['image'].split('@')[0]}")
        if not any(item["host"].endswith(suffix) for suffix in host_suffixes):
            raise ValueError(f"host is not an approved Jupyter domain: {item['host']}")


def _dig(data: dict, path: tuple):
    for key in path:
        if not isinstance(data, dict) or key not in data:
            return None
        data = data[key]
    return data


def validate_against_applied(payload: dict, applied: dict | None) -> None:
    """Compare with the last successfully applied request of the same task."""
    if applied is None:
        if payload["action"] == "UPDATE":
            raise ValueError("no applied state for this task; submit action=CREATE first")
        return
    for path in IMMUTABLE_FIELDS:
        if _dig(payload, path) != _dig(applied, path):
            raise ValueError(f"{'.'.join(path)} cannot change after the first apply")
    if payload["workload"]["storage"]["size_gb"] < applied["workload"]["storage"]["size_gb"]:
        raise ValueError("workload.storage.size_gb cannot shrink")
    if payload["project"].get("create_project", False) != applied["project"].get("create_project", False):
        raise ValueError("project.create_project cannot change after the first apply")


def load_applied_request(task: str) -> dict | None:
    """The runner writes this object after a fully successful apply."""
    blob = storage.Client().bucket(os.environ["BUNDLE_BUCKET"]).blob(f"tasks/{task}/applied-request.json")
    if not blob.exists():
        return None
    return json.loads(blob.download_as_bytes())


def download_source_archive(source: dict, destination: Path) -> Path:
    bucket_name, object_name = parse_gs_uri(source["archive_uri"])
    if bucket_name != os.environ["BUNDLE_BUCKET"]:
        raise ValueError("source archive must use the internal bundle bucket")
    if not object_name.startswith("platform-releases/"):
        raise ValueError("source archive must use platform-releases/")
    blob = storage.Client().bucket(bucket_name).blob(
        object_name, generation=int(source["generation"])
    )
    raw = blob.download_as_bytes()
    actual_hash = hashlib.sha256(raw).hexdigest()
    if not hmac.compare_digest(actual_hash, source["sha256"].lower()):
        raise ValueError("source archive sha256 mismatch")

    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        base = destination.resolve()
        for member in archive.infolist():
            target = (destination / member.filename).resolve()
            if base not in target.parents and target != base:
                raise ValueError("unsafe source archive path")
        archive.extractall(destination)
    return destination


def stage_variables(payload: dict) -> dict[str, dict]:
    task = payload["task"]
    project = payload["project"]
    data = payload["data"]
    identity = payload["identity"]
    region = payload["gke"]["location"]
    tf_manager = f"{os.environ.get('TF_MANAGER_SA', 'tf-manager')}@{os.environ['SBX_GKE_PROJECT']}.iam.gserviceaccount.com"

    return {
        "05-group": {
            "identity_project_id": os.environ["SBX_GKE_PROJECT"],
            "customer_id": os.environ.get("CLOUD_IDENTITY_CUSTOMER_ID", ""),
            "group_email": identity["group_email"],
            "display_name": task["display_name"],
            "members": identity.get("members", []),
        },
        "10-project": {
            "create_project": project.get("create_project", False),
            "project_id": project["project_id"], "project_name": project.get("project_name"),
            "folder_id": project.get("folder_id"), "org_id": project.get("org_id"),
            "billing_account": project.get("billing_account"),
            "task_name": task["name"], "expires_on": task["expires_on"], "region": region,
            "project_iam_service_account": tf_manager,
            "data_admin_service_account": tf_manager,
        },
        "30-project-iam": {
            "project_id": project["project_id"], "group_email": identity["group_email"],
        },
        "40-data": {
            "project_id": project["project_id"], "region": region, "task_name": task["name"],
            "group_email": identity["group_email"],
            "bigquery_dataset": data.get("bigquery_dataset"), "gcs_bucket": data.get("gcs_bucket"),
            "jupyter_gsa_email": identity["jupyter_gsa"]["email"],
            "create_jupyter_gsa": identity["jupyter_gsa"]["create"],
            "datalake_datasets": data.get("datalake_datasets", []),
        },
        "50-workload": workload_variables(payload),
    }


def zip_directory(source: Path) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(source))
    return output.getvalue()


def write_static_data_imports(work: Path, payload: dict) -> None:
    """Write literal import IDs for data resources that were approved for adoption.

    Import block IDs are written as literals so the bundle is self-contained and
    reference input variables. This generated file is included only in the
    request-specific bundle, so its values are already approved and immutable.
    """
    if not payload["data"].get("adopt_existing_resources", False):
        return

    project_id = payload["project"]["project_id"]
    dataset_id = payload["data"]["bigquery_dataset"]
    bucket_name = payload["data"]["gcs_bucket"]
    imports = (
        "import {\n"
        "  to = google_bigquery_dataset.sandbox\n"
        f"  id = {json.dumps(f'projects/{project_id}/datasets/{dataset_id}')}\n"
        "}\n\n"
        "import {\n"
        "  to = google_storage_bucket.sandbox\n"
        f"  id = {json.dumps(bucket_name)}\n"
        "}\n"
    )
    (work / "imports.tf").write_text(imports, encoding="utf-8")


def write_static_group_imports(work: Path, payload: dict) -> None:
    """Import approved existing group/memberships with Terraform 1.5 literal IDs."""
    identity = payload["identity"]
    group_name = identity.get("group_resource_name")
    if not group_name:
        return
    blocks = [
        'import {\n  to = google_cloud_identity_group.sandbox\n'
        f'  id = {json.dumps(group_name)}\n}}\n'
    ]
    for email, name in sorted(identity.get("membership_resource_names", {}).items()):
        blocks.append(
            'import {\n'
            f'  to = google_cloud_identity_group_membership.member[{json.dumps(email)}]\n'
            f'  id = {json.dumps(name)}\n}}\n'
        )
    (work / "imports.tf").write_text("\n".join(blocks), encoding="utf-8")


def upload_immutable(blob, content: bytes, content_type: str) -> None:
    try:
        blob.upload_from_string(content, content_type=content_type, if_generation_match=0)
    except PreconditionFailed:
        # A repeated call for the same approved request and immutable release
        # reuses the already-created bundle.
        return


def assemble_bundles(payload: dict, raw_request: bytes) -> tuple[str, dict]:
    request_id = payload["request_id"]
    release_id = payload["source"]["release_id"]
    prefix = f"requests/{request_id}/{release_id}"
    bucket = storage.Client().bucket(os.environ["BUNDLE_BUCKET"])
    variables = stage_variables(payload)
    manifest = {"request_id": request_id, "release_id": release_id, "bundles": {}}

    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        source_root = download_source_archive(payload["source"], temp / "source")
        deployment_root = source_root / SOURCE_DEPLOYMENTS_DIR
        for stage in enabled_stages(payload):
            archive_name = STAGES[stage][0]
            source = deployment_root / stage
            if not source.is_dir():
                raise ValueError(f"missing Terraform root module: {stage}")
            work = temp / "work" / stage
            shutil.copytree(source, work)
            if stage == "05-group":
                write_static_group_imports(work, payload)
            if stage == "40-data":
                write_static_data_imports(work, payload)
            (work / "terraform.auto.tfvars.json").write_text(
                json.dumps(variables[stage], indent=2, sort_keys=True), encoding="utf-8"
            )
            archive = zip_directory(work)
            object_name = f"{prefix}/{archive_name}"
            upload_immutable(bucket.blob(object_name), archive, "application/zip")
            manifest["bundles"][stage] = {
                "uri": f"gs://{bucket.name}/{object_name}",
                "sha256": hashlib.sha256(archive).hexdigest(),
            }

        upload_immutable(bucket.blob(f"{prefix}/approved-request.json"), raw_request, "application/json")
        upload_immutable(
            bucket.blob(f"{prefix}/manifest.json"),
            json.dumps(manifest, indent=2, sort_keys=True).encode(),
            "application/json",
        )
    return f"gs://{bucket.name}/{prefix}", manifest


def start_build(payload: dict, request_uri: str, bundle_prefix: str):
    """One Cloud Build on the private pool:
      step terraform : Infrastructure Manager apply for each stage, in order
      step alb       : runner image attaches the task to the shared ALB
    """
    project = os.environ["SBX_GKE_PROJECT"]
    region = os.environ["GCP_REGION"]
    task = payload["task"]["name"]
    request_id = payload["request_id"]
    release_id = payload["source"]["release_id"]
    worker_pool = os.environ["WORKER_POOL"]
    tf_manager = f"{os.environ.get('TF_MANAGER_SA', 'tf-manager')}@{project}.iam.gserviceaccount.com"
    service_account = f"projects/{project}/serviceAccounts/{tf_manager}"

    commands = ["set -euo pipefail"]
    for stage in enabled_stages(payload):
        archive = STAGES[stage][0]
        # SERVICE_MAINTAINED serves only Google-maintained providers. 50-workload
        # also needs hashicorp/kubernetes, which that mirror rejects (400), so it
        # downloads from registry.terraform.io through the pool's public egress.
        provider_source = "" if stage == "50-workload" else "--provider-source=SERVICE_MAINTAINED "
        commands.append(
            "gcloud infra-manager deployments apply "
            f"\"projects/{project}/locations/{region}/deployments/{deployment_name(payload, stage)}\" "
            f"--service-account=\"{service_account}\" "
            f"--gcs-source=\"{bundle_prefix}/{archive}\" "
            f"--worker-pool=\"{worker_pool}\" "
            f"--tf-version-constraint=\"{os.environ.get('TF_VERSION_CONSTRAINT', '1.5.7')}\" "
            f"{provider_source}"
            f"--labels=\"task={task},stage={STAGES[stage][1]}\" "
            f"--annotations=\"request_id={request_id}\" "
            "--quiet"
        )
        if stage == "05-group":
            # Group visibility in IAM may lag creation. Failures stop the build.
            commands.append("sleep 60")

    runner_image = f"{os.environ['RUNNER_IMAGE_REPO']}:{release_id}"
    env = [
        f"SBX_GKE_PROJECT={project}",
        f"GCP_REGION={region}",
        f"BUNDLE_BUCKET={os.environ['BUNDLE_BUCKET']}",
        f"SHARED_URL_MAP_NAME={os.environ['SHARED_URL_MAP_NAME']}",
        f"SHARED_HEALTH_CHECK_NAME={os.environ['SHARED_HEALTH_CHECK_NAME']}",
        f"GKE_ENDPOINT_MODE={os.environ.get('GKE_ENDPOINT_MODE', 'dns')}",
        "KUBECONFIG=/workspace/kubeconfig",
    ]
    build_spec = {
        "steps": [
            {
                "id": "terraform",
                "name": "gcr.io/google.com/cloudsdktool/cloud-sdk:slim",
                "entrypoint": "bash",
                "args": ["-ceu", "\n".join(commands)],
            },
            {
                "id": "alb",
                "name": runner_image,
                "entrypoint": "python3",
                "args": ["/opt/sandbox/reconcile_postdeploy.py", f"{bundle_prefix}/approved-request.json"],
                "env": env,
            },
        ],
        "options": {"logging": "CLOUD_LOGGING_ONLY", "pool": {"name": worker_pool}},
        "service_account": service_account,
        "timeout": "7200s",
        "tags": [f"task-{task}", request_id.lower()[:128]],
    }
    build = cloudbuild_v1.Build(build_spec)
    client = cloudbuild_v1.CloudBuildClient(
        client_options=ClientOptions(
            api_endpoint=f"{region}-cloudbuild.googleapis.com"
        )
    )
    return client.create_build(request=cloudbuild_v1.CreateBuildRequest(
        parent=f"projects/{project}/locations/{region}",
        project_id=project,
        build=build,
    ))


def start_build_once(payload: dict, request_uri: str, bundle_prefix: str) -> tuple[str, bool]:
    bucket_name, prefix = parse_gs_uri(bundle_prefix)
    lock = storage.Client().bucket(bucket_name).blob(f"{prefix}/dispatch.json")
    starting = json.dumps({"request_id": payload["request_id"], "status": "STARTING"}).encode()
    try:
        lock.upload_from_string(starting, content_type="application/json", if_generation_match=0)
    except PreconditionFailed:
        previous = json.loads(lock.download_as_bytes())
        operation = previous.get("operation")
        if operation:
            return operation, True
        raise ValueError("this approved request is already being dispatched")

    lock.reload()
    generation = lock.generation
    try:
        operation = start_build(payload, request_uri, bundle_prefix).operation.name
        dispatched = json.dumps({
            "request_id": payload["request_id"], "status": "DISPATCHED", "operation": operation,
        }).encode()
        lock.upload_from_string(
            dispatched, content_type="application/json", if_generation_match=generation
        )
        return operation, False
    except Exception:
        lock.delete(if_generation_match=generation)
        raise


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/provision")
def provision():
    body = request.get_json(silent=True) or {}
    request_id = body.get("request_id", "")
    if not REQUEST_RE.fullmatch(request_id):
        return jsonify(error="invalid request_id"), 400
    try:
        request_uri = body["approved_json_uri"]
        _, request_object = parse_gs_uri(request_uri)
        if request_object != f"approved/{request_id}/request.json":
            raise ValueError("approved_json_uri must use approved/<request_id>/request.json")
        payload, raw = download_approved_request(request_uri, int(body["generation"]), body["sha256"])
        if payload["request_id"] != request_id:
            raise ValueError("request_id does not match approved JSON")
        validate_against_applied(payload, load_applied_request(payload["task"]["name"]))
        bundle_prefix, manifest = assemble_bundles(payload, raw)
        operation, duplicate = start_build_once(payload, request_uri, bundle_prefix)
    except (KeyError, TypeError, ValueError) as error:
        return jsonify(error=str(error), request_id=request_id), 400
    except Exception:
        app.logger.exception("provisioning request failed")
        return jsonify(error="internal provisioning error", request_id=request_id), 500
    return jsonify(
        request_id=request_id, status="ACCEPTED", bundle_prefix=bundle_prefix,
        operation=operation, duplicate=duplicate, bundle_count=len(manifest["bundles"]),
    ), 202
