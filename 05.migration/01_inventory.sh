#!/usr/bin/env bash
# Read-only. Lists every notebook VM of the given task projects as CSV:
#   project,vm,zone,status,machine_type,service_account,data_disk,data_disk_gb,internal_ip
# The data disk is the first non-boot disk. Review the CSV by hand before use:
# tasks with more than one VM or more than one data disk need a decision.
#
# Usage: ./01_inventory.sh projects.txt > inventory.csv      (one project id per line)
set -euo pipefail
projects_file="${1:?Usage: $0 PROJECTS_FILE}"

echo "project,vm,zone,status,machine_type,service_account,data_disk,data_disk_gb,internal_ip"
while IFS= read -r project; do
  [[ -z "$project" || "$project" == \#* ]] && continue
  gcloud compute instances list --project="$project" --format=json | python3 -c '
import json, sys
project = sys.argv[1]
for vm in json.load(sys.stdin):
    data = [d for d in vm.get("disks", []) if not d.get("boot")]
    disk = data[0] if data else {}
    print(",".join([
        project, vm["name"], vm["zone"].rsplit("/", 1)[-1], vm["status"],
        vm["machineType"].rsplit("/", 1)[-1],
        (vm.get("serviceAccounts") or [{}])[0].get("email", ""),
        disk.get("source", "").rsplit("/", 1)[-1], str(disk.get("diskSizeGb", "")),
        vm["networkInterfaces"][0].get("networkIP", ""),
    ]))
' "$project"
done < "$projects_file"
