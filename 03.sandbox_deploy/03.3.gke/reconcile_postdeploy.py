#!/usr/bin/env python3
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ZONAL_NEG_RE = re.compile(r"/zones/([^/]+)/networkEndpointGroups/([^/]+)$")


def run(args, *, env=None, quiet=False):
    command = " ".join(args)
    if not quiet:
        print(f"[RUN] {command}", flush=True)
    result = subprocess.run(
        args,
        text=True,
        check=False,
        capture_output=True,
        env=env,
    )
    if result.returncode:
        raise RuntimeError(
            f"command failed ({result.returncode}): {command}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result.stdout.strip()


def gcloud(*args, env=None, quiet=False):
    return run(["gcloud", *args], env=env, quiet=quiet)


def gcloud_describe_or_none(resource_args, *, env=None):
    args = ["gcloud", *resource_args, "--format=json"]
    result = subprocess.run(
        args,
        text=True,
        check=False,
        capture_output=True,
        env=env,
    )
    if result.returncode == 0:
        return json.loads(result.stdout or "{}")
    text = f"{result.stdout}\n{result.stderr}".lower()
    if "not found" in text or "was not found" in text:
        return None
    raise RuntimeError(
        f"command failed ({result.returncode}): {' '.join(args)}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


SERVICE_NAME = "jupyter"


def verify_workload_identity(request, env):
    task = request["task"]["name"]
    namespace = request["workload"]["namespace"]
    ksa = f"ksa-jupyter-{task}"
    expected_gsa = request["identity"]["jupyter_gsa"]["email"]
    actual_gsa = run([
        "kubectl", "get", "serviceaccount", ksa,
        f"--namespace={namespace}",
        "--output=jsonpath={.metadata.annotations.iam\\.gke\\.io/gcp-service-account}",
    ], env=env)
    if actual_gsa != expected_gsa:
        raise RuntimeError(
            f"KSA/GSA mismatch: {namespace}/{ksa} -> {actual_gsa!r}, expected {expected_gsa!r}"
        )
    print(f"[OK] Workload Identity: {namespace}/{ksa} -> {expected_gsa}", flush=True)


def resolve_neg_backends(request, port, env):
    """Zonal NEGs GKE created for one Service port."""
    namespace = request["workload"]["namespace"]
    gke_project = request["gke"]["project_id"]
    last_status = None

    for _ in range(30):
        service = json.loads(run([
            "kubectl", "get", "service", SERVICE_NAME,
            f"--namespace={namespace}", "--output=json",
        ], env=env, quiet=True))
        raw_status = service.get("metadata", {}).get("annotations", {}).get("cloud.google.com/neg-status")
        if raw_status:
            last_status = json.loads(raw_status)
            neg_name = last_status.get("network_endpoint_groups", {}).get(str(port))
            zones = last_status.get("zones", [])
            if neg_name and zones:
                print(f"[OK] NEG ready: port={port} {neg_name} zones={','.join(zones)}", flush=True)
                return [{
                    "name": neg_name, "zone": zone,
                    "self_link": f"projects/{gke_project}/zones/{zone}/networkEndpointGroups/{neg_name}",
                } for zone in zones]
        time.sleep(10)

    raise RuntimeError(
        f"GKE did not publish a NEG for {namespace}/{SERVICE_NAME}:{port} within 5 minutes; "
        f"last_status={last_status}"
    )


def backend_name(task, container):
    return f"bes-sbx-{task}-{container}"[:63].rstrip("-")


def ensure_backend_service(project, name, health_check_name, neg_backends, env):
    region = env["GCP_REGION"]
    # Notebook kernels use WebSocket. On an Application Load Balancer the backend
    # timeout is the maximum lifetime of such a connection.
    timeout = f"--timeout={env.get('BACKEND_TIMEOUT_SEC', '86400')}s"
    describe_args = ("compute", "backend-services", "describe", name, f"--project={project}", f"--region={region}")
    current = gcloud_describe_or_none(describe_args, env=env)

    if current is None:
        gcloud(
            "compute", "backend-services", "create", name,
            f"--project={project}", f"--region={region}",
            "--load-balancing-scheme=INTERNAL_MANAGED", "--protocol=HTTP", timeout,
            f"--health-checks={health_check_name}", f"--health-checks-region={region}",
            "--quiet", env=env,
        )
        current = gcloud_describe_or_none(describe_args, env=env)
        print(f"[OK] Created backend service {name}", flush=True)
    else:
        if current.get("loadBalancingScheme") != "INTERNAL_MANAGED" or current.get("protocol") != "HTTP":
            raise RuntimeError(f"backend {name} exists with an unexpected scheme or protocol")
        print(f"[OK] Reused backend service {name}", flush=True)

    desired = {item["self_link"] for item in neg_backends}
    attached = {b.get("group") for b in current.get("backends", []) if b.get("group")}
    attached_suffix = {g.split("/compute/v1/")[-1] for g in attached}

    # The backend service belongs to one container of one task, so NEGs left by
    # an earlier generation are stale and removed.
    for group in sorted(attached_suffix - desired):
        match = ZONAL_NEG_RE.search(group)
        if not match:
            raise RuntimeError(f"unexpected backend group on {name}: {group}")
        zone, neg_name = match.groups()
        gcloud(
            "compute", "backend-services", "remove-backend", name,
            f"--project={project}", f"--region={region}",
            f"--network-endpoint-group={neg_name}", f"--network-endpoint-group-zone={zone}",
            "--quiet", env=env,
        )
        print(f"[OK] Removed stale NEG backend {zone}/{neg_name}", flush=True)

    for backend in neg_backends:
        if backend["self_link"] in attached_suffix:
            continue
        gcloud(
            "compute", "backend-services", "add-backend", name,
            f"--project={project}", f"--region={region}",
            f"--network-endpoint-group={backend['name']}",
            f"--network-endpoint-group-zone={backend['zone']}",
            "--balancing-mode=RATE", "--max-rate-per-endpoint=100", "--capacity-scaler=1.0",
            "--quiet", env=env,
        )
        print(f"[OK] Attached NEG backend {backend['zone']}/{backend['name']}", flush=True)
    return name


def wait_for_backend_healthy(project, name, env):
    last_states = []
    for _ in range(60):
        raw = gcloud(
            "compute", "backend-services", "get-health", name,
            f"--project={project}", f"--region={env['GCP_REGION']}", "--format=json",
            env=env, quiet=True,
        )
        states = [
            health.get("healthState", "UNKNOWN")
            for entry in json.loads(raw or "[]")
            for health in entry.get("status", {}).get("healthStatus", [])
        ]
        last_states = states
        if "HEALTHY" in states:
            print(f"[OK] Backend {name} HEALTHY", flush=True)
            return
        time.sleep(10)
    raise RuntimeError(f"backend {name} did not become HEALTHY within 10 minutes; last={last_states}")


# ---------------------------------------------------------------------------
# URL map. One shared map serves every task, so each change is a
# read-modify-write guarded by the map's fingerprint.
# ---------------------------------------------------------------------------
def _same_service(left, right):
    return bool(left) and bool(right) and (left == right or left.endswith("/" + right) or right.endswith("/" + left))


def _matcher_name(prefix, value):
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return f"{prefix}-{slug}"[:63].rstrip("-")


def merge_route(config, host, base_path, backend_uri):
    """Add or confirm one route in a URL map dict. Returns True when it changed.

    base_path "/"   -> the whole host belongs to this backend (host scheme)
    otherwise       -> host is shared; <base_path>* goes to this backend (path scheme)
    Raises when the host or path already belongs to a different backend.
    """
    host_rules = config.setdefault("hostRules", [])
    matchers = config.setdefault("pathMatchers", [])
    rule = next((item for item in host_rules if host in item.get("hosts", [])), None)
    changed = False

    if base_path == "/":
        if rule is None:
            name = _matcher_name("t", host)
            matchers.append({"name": name, "defaultService": backend_uri})
            host_rules.append({"hosts": [host], "pathMatcher": name})
            return True
        matcher = next(item for item in matchers if item["name"] == rule["pathMatcher"])
        if matcher.get("pathRules") or not _same_service(matcher.get("defaultService"), backend_uri):
            raise RuntimeError(f"host {host} is already routed elsewhere ({matcher['name']})")
        return False

    if rule is None:
        name = _matcher_name("h", host)
        # Unknown paths on a shared host fall through to the map's default backend.
        matcher = {"name": name, "defaultService": config["defaultService"], "pathRules": []}
        matchers.append(matcher)
        host_rules.append({"hosts": [host], "pathMatcher": name})
        changed = True
    else:
        matcher = next(item for item in matchers if item["name"] == rule["pathMatcher"])

    paths = [base_path.rstrip("/"), base_path + "*"]
    path_rules = matcher.setdefault("pathRules", [])
    for item in path_rules:
        if set(item.get("paths", [])) & set(paths):
            if not _same_service(item.get("service"), backend_uri):
                raise RuntimeError(f"path {base_path} on {host} is already routed to {item.get('service')}")
            return changed
    path_rules.append({"paths": paths, "service": backend_uri})
    return True


def reconcile_url_map(project, url_map_name, routes, env):
    """routes: list of (host, base_path, backend service name)."""
    import yaml

    region = env["GCP_REGION"]
    for attempt in range(6):
        config = yaml.safe_load(gcloud(
            "compute", "url-maps", "export", url_map_name,
            f"--project={project}", f"--region={region}", env=env, quiet=True,
        ))
        changed = False
        for host, base_path, backend in routes:
            backend_uri = f"projects/{project}/regions/{region}/backendServices/{backend}"
            changed = merge_route(config, host, base_path, backend_uri) or changed
        if not changed:
            print(f"[OK] URL map already routes {len(routes)} route(s)", flush=True)
            return
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as stream:
            yaml.safe_dump(config, stream)
        try:
            # The exported fingerprint makes the import fail if another task
            # changed the map in between; then re-read and merge again.
            gcloud(
                "compute", "url-maps", "import", url_map_name,
                f"--project={project}", f"--region={region}",
                f"--source={stream.name}", "--quiet", env=env,
            )
            for host, base_path, backend in routes:
                print(f"[OK] URL map route: https://{host}{base_path} -> {backend}", flush=True)
            return
        except RuntimeError as error:
            if "fingerprint" not in str(error).lower() and "412" not in str(error):
                raise
            print(f"[RETRY] URL map changed concurrently (attempt {attempt + 1})", flush=True)
            time.sleep(5 + attempt * 5)
    raise RuntimeError("URL map update kept conflicting with other changes; rerun the request")


def record_applied_request(request_path, request, env):
    """Mark this request as the task's applied state. The provisioner compares
    the next request against it (immutable fields, no disk shrink)."""
    bucket = env["BUNDLE_BUCKET"]
    task = request["task"]["name"]
    gcloud("storage", "cp", str(request_path), f"gs://{bucket}/tasks/{task}/applied-request.json", env=env)


def load_request_and_credentials(source, env):
    """Copy the approved request from GCS and get cluster credentials.

    Terraform ran inside Infrastructure Manager, so this step starts with no
    local files and no kubeconfig."""
    request_path = Path("/workspace/approved-request.json")
    if source.startswith("gs://"):
        gcloud("storage", "cp", source, str(request_path), env=env)
    else:
        request_path = Path(source)
    request = json.loads(request_path.read_text(encoding="utf-8"))
    gke = request["gke"]
    gcloud(
        "container", "clusters", "get-credentials", gke["cluster_name"],
        f"--project={gke['project_id']}", f"--location={gke['location']}",
        "--dns-endpoint" if env.get("GKE_ENDPOINT_MODE", "dns") == "dns" else "--internal-ip",
        env=env,
    )
    return request_path, request


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: reconcile_postdeploy.py APPROVED_REQUEST_(GCS_URI|JSON)")

    env = dict(os.environ)
    env.pop("CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT", None)
    request_path, request = load_request_and_credentials(sys.argv[1], env)
    task = request["task"]["name"]
    project = request["gke"]["project_id"]

    verify_workload_identity(request, env)

    # The shared ALB (VIP, proxies, URL map, health check) is built once by hand.
    # A task adds: backend service + NEG per container, and its URL map routes.
    routes, backends = [], []
    for container in request["workload"]["containers"]:
        negs = resolve_neg_backends(request, container["hub_port"], env)
        name = ensure_backend_service(
            project, backend_name(task, container["name"]), env["SHARED_HEALTH_CHECK_NAME"], negs, env)
        backends.append(name)
        routes.append((container["host"], container["base_path"], name))

    reconcile_url_map(project, env["SHARED_URL_MAP_NAME"], routes, env)

    if request["workload"]["running"]:
        for name in backends:
            wait_for_backend_healthy(project, name, env)
    else:
        print("[OK] task is stopped (replicas=0); health wait skipped", flush=True)

    record_applied_request(request_path, request, env)
    print(f"[OK] Sandbox {task} reconcile complete", flush=True)


if __name__ == "__main__":
    main()
