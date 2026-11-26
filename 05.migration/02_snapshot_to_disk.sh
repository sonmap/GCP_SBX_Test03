#!/usr/bin/env bash
# Moves one task's data disk next to the GKE cluster, whole and unchanged:
#   VM stop -> snapshot (task project) -> new disk (GKE project, same zone)
# The VM and its original disk are left in place: starting the VM again is the rollback.
# Re-running is safe; existing snapshot or disk is reused.
#
# Usage: ./02_snapshot_to_disk.sh TASK TASK_PROJECT VM ZONE DATA_DISK GKE_PROJECT [DISK_TYPE]
set -euo pipefail
task="${1:?TASK}"; task_project="${2:?TASK_PROJECT}"; vm="${3:?VM}"; zone="${4:?ZONE}"
data_disk="${5:?DATA_DISK}"; gke_project="${6:?GKE_PROJECT}"; disk_type="${7:-pd-balanced}"

snapshot="snap-sbx-${task}-data-$(date +%Y%m%d)"
new_disk="disk-sbx-${task}-data"

status="$(gcloud compute instances describe "$vm" --project="$task_project" --zone="$zone" --format='value(status)')"
if [[ "$status" != "TERMINATED" ]]; then
  echo "[RUN] stop $vm (status=$status)"
  gcloud compute instances stop "$vm" --project="$task_project" --zone="$zone" --quiet
fi

if ! gcloud compute snapshots describe "$snapshot" --project="$task_project" >/dev/null 2>&1; then
  echo "[RUN] snapshot $data_disk -> $snapshot"
  gcloud compute disks snapshot "$data_disk" --project="$task_project" --zone="$zone" \
    --snapshot-names="$snapshot" --labels="task=${task},purpose=gke-migration" --quiet
fi

# The CSI driver can only attach disks of the cluster's own project.
if ! gcloud compute disks describe "$new_disk" --project="$gke_project" --zone="$zone" >/dev/null 2>&1; then
  echo "[RUN] create $new_disk in $gke_project"
  gcloud compute disks create "$new_disk" --project="$gke_project" --zone="$zone" --type="$disk_type" \
    --source-snapshot="projects/${task_project}/global/snapshots/${snapshot}" \
    --labels="task=${task},origin=migrated" --quiet
fi

size="$(gcloud compute disks describe "$new_disk" --project="$gke_project" --zone="$zone" --format='value(sizeGb)')"
echo "existing_disk=$new_disk"
echo "zone=$zone"
echo "size_gb=$size"
