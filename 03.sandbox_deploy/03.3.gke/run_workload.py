#!/usr/bin/env python3
"""Apply one task's Kubernetes workload with Terraform (stage 50-workload).

Input is the approved full-state request. The same command creates a task,
stops or starts it, resizes it, or swaps its image: the request changes,
Terraform works out the difference against the task's own state.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

MODULE_DIR = Path(os.environ.get("WORKLOAD_MODULE_DIR", "/opt/sandbox/terraform/50-workload"))
WORKSPACE = Path(os.environ.get("WORKSPACE_DIR", "/workspace"))

# Deleting any of these means data loss or an outage that a request should
# never cause. A plan that tries is rejected before apply.
PROTECTED_TYPES = {
    "kubernetes_namespace",
    "kubernetes_persistent_volume",
    "kubernetes_persistent_volume_claim",
    "kubernetes_service",
    "kubernetes_service_account",
    "google_service_account_iam_member",
}


def run(args, *, env=None, cwd=None, capture=True):
    print(f"[RUN] {' '.join(args)}", flush=True)
    result = subprocess.run(args, text=True, check=False, capture_output=capture, env=env, cwd=cwd)
    if result.returncode:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(args)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return (result.stdout or "").strip()


def workload_tfvars(request: dict, env: dict) -> dict:
    """Translate the request into 50-workload variables."""
    task = request["task"]
    workload = request["workload"]
    storage = workload["storage"]
    existing_disk = storage.get("existing_disk", "")
    default_class = env.get("STATIC_STORAGE_CLASS", "sbx-static-pd") if existing_disk else env.get(
        "DYNAMIC_STORAGE_CLASS", "standard-rwo")
    variables = {
        "kubeconfig_path": env["KUBECONFIG"],
        "gke_project_id": request["gke"]["project_id"],
        "wi_impersonate_service_account": env.get("WI_IMPERSONATE_SA", ""),
        "task_name": task["name"],
        "task_project_id": request["project"]["project_id"],
        "area": workload["area"],
        "namespace": workload["namespace"],
        "origin": task["origin"],
        "request_id": request["request_id"],
        "jupyter_gsa_email": request["identity"]["jupyter_gsa"]["email"],
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
        "hub_oauth_base_url": env.get("HUB_OAUTH_BASE_URL", ""),
        "portal_members_api": env.get("PORTAL_MEMBERS_API", ""),
        "hub_admin_users": [u.strip() for u in env.get("HUB_ADMIN_USERS", "").split(",") if u.strip()],
    }
    return variables


def destructive_changes(plan: dict) -> list[str]:
    """Addresses of protected resources the plan would delete or replace."""
    blocked = []
    for change in plan.get("resource_changes", []):
        actions = change.get("change", {}).get("actions", [])
        if "delete" in actions and change.get("type") in PROTECTED_TYPES:
            blocked.append(f"{change['address']} ({'/'.join(actions)})")
    return sorted(blocked)


def ensure_oauth_secret(request: dict, env: dict) -> None:
    """Copy the OAuth client from Secret Manager into the task namespace.

    Done with kubectl, outside Terraform, so the values never enter state."""
    namespace = request["workload"]["namespace"]
    project = env["SBX_GKE_PROJECT"]
    files = {}
    for key, secret in (("client_id", env.get("OAUTH_CLIENT_ID_SECRET", "jupyter-oauth-client-id")),
                        ("client_secret", env.get("OAUTH_CLIENT_SECRET_SECRET", "jupyter-oauth-client-secret"))):
        result = subprocess.run(
            ["gcloud", "secrets", "versions", "access", "latest", f"--secret={secret}", f"--project={project}"],
            text=True, capture_output=True, check=False, env=env)
        if result.returncode:
            raise RuntimeError(f"cannot read secret {secret}: {result.stderr.strip()}")
        files[key] = result.stdout.strip()
    manifest = {
        "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
        "metadata": {"name": "hub-oauth", "namespace": namespace},
        "stringData": files,
    }
    print(f"[RUN] kubectl apply secret {namespace}/hub-oauth", flush=True)
    result = subprocess.run(["kubectl", "apply", "-f", "-"], input=json.dumps(manifest), text=True,
                            capture_output=True, check=False, env=env)
    if result.returncode:
        raise RuntimeError(f"kubectl apply secret failed: {result.stderr.strip()}")


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: run_workload.py APPROVED_REQUEST_GCS_URI")

    env = dict(os.environ)
    env.pop("CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT", None)
    env.setdefault("KUBECONFIG", str(WORKSPACE / "kubeconfig"))

    request_file = WORKSPACE / "approved-request.json"
    run(["gcloud", "storage", "cp", sys.argv[1], str(request_file)], env=env)
    request = json.loads(request_file.read_text(encoding="utf-8"))
    task = request["task"]["name"]
    gke = request["gke"]

    run([
        "gcloud", "container", "clusters", "get-credentials", gke["cluster_name"],
        f"--project={gke['project_id']}", f"--location={gke['location']}",
        "--dns-endpoint" if env.get("GKE_ENDPOINT_MODE", "internal") == "dns" else "--internal-ip",
    ], env=env)

    work = WORKSPACE / "50-workload"
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(MODULE_DIR, work)
    (work / "terraform.auto.tfvars.json").write_text(
        json.dumps(workload_tfvars(request, env), indent=2, sort_keys=True), encoding="utf-8")

    run(["terraform", "init", "-input=false", "-no-color",
         f"-backend-config=bucket={env['TF_STATE_BUCKET']}",
         f"-backend-config=prefix=tasks/{task}/50-workload"], env=env, cwd=work)
    run(["terraform", "plan", "-input=false", "-no-color", "-lock-timeout=300s", "-out=tfplan"], env=env, cwd=work)
    plan = json.loads(run(["terraform", "show", "-json", "tfplan"], env=env, cwd=work))

    blocked = destructive_changes(plan)
    if blocked:
        raise SystemExit(
            "[STOP] the plan would delete protected resources; nothing was applied:\n  "
            + "\n  ".join(blocked))

    # First apply of a task: the namespace must exist before the Secret, and the
    # Secret before the Pod. Create the namespace alone, then everything else.
    if request["workload"].get("hub_config", "MANAGED") == "MANAGED":
        run(["terraform", "apply", "-input=false", "-no-color", "-auto-approve", "-lock-timeout=300s",
             "-target=kubernetes_namespace.task"], env=env, cwd=work)
        ensure_oauth_secret(request, env)
        run(["terraform", "apply", "-input=false", "-no-color", "-auto-approve", "-lock-timeout=300s"],
            env=env, cwd=work)
    else:
        run(["terraform", "apply", "-input=false", "-no-color", "-lock-timeout=300s", "tfplan"], env=env, cwd=work)
    print(f"[OK] workload applied: task={task} running={request['workload']['running']}", flush=True)


if __name__ == "__main__":
    main()
