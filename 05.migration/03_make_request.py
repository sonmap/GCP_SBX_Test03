#!/usr/bin/env python3
"""Build the request JSON of one migrated task (schema 2.0, origin MIGRATED).

The result has the same format the portal will send for every later change of
this task (stop, start, resize, image swap), so migration and operation share
one path. Values that differ per environment come from --defaults (JSON file).

Usage:
  ./03_make_request.py --defaults defaults.json --task rsch --project gcp-sbx-edp-rsch \
     --area general --gsa sa-sbx-rsch-notebook-1-user@gcp-sbx-edp-rsch.iam.gserviceaccount.com \
     --disk disk-sbx-rsch-data --zone asia-northeast3-a --size-gb 100 \
     --image REGION-docker.pkg.dev/PROJECT/REPO/IMAGE@sha256:... > request.json
"""
import argparse
import datetime
import json
import sys


def build(args, defaults):
    containers = []
    for index, image in enumerate(args.image):
        single = len(args.image) == 1
        name = "jupyter" if single else f"jupyter-{index + 1}"
        suffix = "" if single else f"v{index + 1}/"
        containers.append({
            "name": name,
            "image": image,
            "hub_port": 8081 + index,
            "host": defaults["host_template"].format(task=args.task, area=args.area),
            "base_path": defaults["base_path_template"].format(task=args.task, area=args.area) + suffix,
        })
    stamp = datetime.datetime.now(datetime.timezone.utc)
    return {
        "schema_version": "2.0",
        "request_id": f"REQ-MIG-{args.task.upper()}-{stamp:%Y%m%d%H%M}",
        "approval": {"status": "APPROVED", "approval_id": args.approval_id,
                     "approved_at": stamp.isoformat(timespec="seconds")},
        "action": "CREATE",
        "task": {"name": args.task, "display_name": args.display_name or f"Sandbox {args.task}",
                 "expires_on": args.expires_on, "origin": "MIGRATED"},
        "identity": {
            "group_email": defaults["group_email_template"].format(task=args.task),
            "manage_group": False,
            "jupyter_gsa": {"email": args.gsa, "create": False},
        },
        "project": {"manage": False, "project_id": args.project},
        "data": {"manage": False},
        "gke": defaults["gke"],
        "workload": {
            "area": args.area,
            "namespace": defaults["namespace_template"].format(task=args.task, area=args.area),
            "running": not args.stopped,
            "cpu": args.cpu, "memory_gib": args.memory_gib,
            "hub_config": args.hub_config,
            "storage": {"size_gb": args.size_gb, "zone": args.zone, "existing_disk": args.disk},
            "containers": containers,
        },
        "source": defaults["source"],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--defaults", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--area", required=True)
    parser.add_argument("--gsa", required=True)
    parser.add_argument("--disk", required=True)
    parser.add_argument("--zone", required=True)
    parser.add_argument("--size-gb", type=int, required=True)
    parser.add_argument("--image", action="append", required=True,
                        help="digest-pinned image; repeat for several containers in one Pod")
    parser.add_argument("--cpu", type=int, default=8)
    parser.add_argument("--memory-gib", type=int, default=64)
    parser.add_argument("--hub-config", choices=["MANAGED", "IMAGE"], default="MANAGED")
    parser.add_argument("--stopped", action="store_true", help="migrate without starting the Pod")
    parser.add_argument("--approval-id", default="migration")
    parser.add_argument("--expires-on", default="2099-12-31")
    parser.add_argument("--display-name")
    args = parser.parse_args()
    with open(args.defaults, encoding="utf-8") as stream:
        defaults = json.load(stream)
    json.dump(build(args, defaults), sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
