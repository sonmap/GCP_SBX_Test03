locals {
  labels = {
    "sandbox/task"                 = var.task_name
    "sandbox/area"                 = var.area
    "sandbox/origin"               = lower(var.origin)
    "app.kubernetes.io/managed-by" = "terraform"
  }
  selector = { "app" = "jupyter", "sandbox/task" = var.task_name }

  static_disk = var.existing_disk != ""
  ports       = { for c in var.containers : tostring(c.hub_port) => c }

  # CPU and memory are split evenly; Autopilot bills the Pod total.
  per_cpu_milli  = floor(var.cpu * 1000 / length(var.containers))
  per_memory_mib = floor(var.memory_gib * 1024 / length(var.containers))
  per_ephemeral  = floor(var.ephemeral_storage_gib * 1024 / length(var.containers))

  node_selector = merge(
    { "topology.kubernetes.io/zone" = var.zone },
    var.gpu == null ? { "cloud.google.com/compute-class" = var.compute_class } : {
      "cloud.google.com/gke-accelerator" = var.gpu.type
    },
  )
}

# ------------------------------------------------------------------ namespace
resource "kubernetes_namespace" "task" {
  metadata {
    name   = var.namespace
    labels = local.labels
  }
}

# ------------------------------------------------------------------ identity
resource "kubernetes_service_account" "jupyter" {
  metadata {
    name      = var.ksa_name
    namespace = kubernetes_namespace.task.metadata[0].name
    labels    = local.labels
    annotations = {
      "iam.gke.io/gcp-service-account" = var.jupyter_gsa_email
    }
  }
}

# Lets <namespace>/<ksa> act as the task's service account. For migrated tasks
# this is the service account the notebook VM used, so data access is unchanged.
resource "google_service_account_iam_member" "workload_identity" {
  provider           = google.task
  service_account_id = "projects/${var.task_project_id}/serviceAccounts/${var.jupyter_gsa_email}"
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.gke_project_id}.svc.id.goog[${var.namespace}/${var.ksa_name}]"
}

# ------------------------------------------------------------------ storage
# Existing disk: the whole AS-IS data disk, attached as it is. Retain keeps the
# disk when the claim or the volume object is removed.
resource "kubernetes_persistent_volume" "data" {
  count = local.static_disk ? 1 : 0

  metadata {
    name   = "pv-${var.namespace}-data"
    labels = local.labels
  }
  spec {
    capacity                         = { storage = "${var.disk_size_gb}Gi" }
    access_modes                     = ["ReadWriteOnce"]
    persistent_volume_reclaim_policy = "Retain"
    storage_class_name               = var.storage_class
    volume_mode                      = "Filesystem"

    claim_ref {
      namespace = var.namespace
      name      = "data"
    }
    persistent_volume_source {
      csi {
        driver        = "pd.csi.storage.gke.io"
        volume_handle = "projects/${var.gke_project_id}/zones/${var.zone}/disks/${var.existing_disk}"
        fs_type       = "ext4"
      }
    }
    node_affinity {
      required {
        node_selector_term {
          match_expressions {
            key      = "topology.gke.io/zone"
            operator = "In"
            values   = [var.zone]
          }
        }
      }
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "kubernetes_persistent_volume_claim" "data" {
  metadata {
    name      = "data"
    namespace = kubernetes_namespace.task.metadata[0].name
    labels    = local.labels
  }
  spec {
    access_modes       = ["ReadWriteOnce"]
    storage_class_name = var.storage_class
    volume_name        = local.static_disk ? kubernetes_persistent_volume.data[0].metadata[0].name : null
    resources {
      requests = { storage = "${var.disk_size_gb}Gi" }
    }
  }
  # New disks bind when the Pod is scheduled (WaitForFirstConsumer).
  wait_until_bound = false

  lifecycle {
    prevent_destroy = true
  }
}

# ------------------------------------------------------------------ hub config
resource "kubernetes_config_map" "hub_config" {
  for_each = var.hub_config_managed ? local.ports : {}

  metadata {
    name      = "hub-config-${each.value.name}"
    namespace = kubernetes_namespace.task.metadata[0].name
    labels    = local.labels
  }
  data = {
    "jupyterhub_config.py" = templatefile("${path.module}/templates/jupyterhub_config.py.tftpl", {
      hub_port           = each.value.hub_port
      # Each Hub needs its own internal ports when several share one Pod.
      hub_internal_port  = each.value.hub_port + 800
      proxy_api_port     = each.value.hub_port - 80
      base_url           = each.value.base_path
      public_url         = "https://${each.value.host}${each.value.base_path}"
      oauth_base_url     = var.hub_oauth_base_url
      portal_members_api = var.portal_members_api
      admin_users        = jsonencode(var.hub_admin_users)
      task_project_id    = var.task_project_id
      data_mount_path    = var.data_mount_path
    })
  }
}

# ------------------------------------------------------------------ workload
resource "kubernetes_deployment" "jupyter" {
  metadata {
    name      = "jupyter"
    namespace = kubernetes_namespace.task.metadata[0].name
    labels    = local.labels
    annotations = {
      "sandbox/request-id" = var.request_id
    }
  }
  # A stopped task has no Pod; do not wait for a rollout that will not happen.
  wait_for_rollout = false

  spec {
    replicas = var.running ? 1 : 0

    # The disk is ReadWriteOnce. A rolling update would start a second Pod that
    # can never mount it.
    strategy {
      type = "Recreate"
    }
    selector {
      match_labels = local.selector
    }

    template {
      metadata {
        labels = merge(local.labels, local.selector)
        annotations = {
          # Roll the Pod when the rendered Hub config changes.
          "sandbox/hub-config-hash" = var.hub_config_managed ? sha256(jsonencode({ for k, v in kubernetes_config_map.hub_config : k => v.data })) : "image"
        }
      }
      spec {
        service_account_name             = kubernetes_service_account.jupyter.metadata[0].name
        node_selector                    = local.node_selector
        termination_grace_period_seconds = 60
        enable_service_links             = false

        dynamic "container" {
          for_each = var.containers
          content {
            name    = container.value.name
            image   = container.value.image
            command = length(container.value.command) > 0 ? container.value.command : null
            args    = length(container.value.args) > 0 ? container.value.args : null

            port {
              name           = "hub"
              container_port = container.value.hub_port
            }

            # The Hub's pre_spawn_hook reads the project with
            # `gcloud config get-value project`; on a VM that came from metadata.
            env {
              name  = "CLOUDSDK_CORE_PROJECT"
              value = var.task_project_id
            }
            env {
              name  = "GOOGLE_CLOUD_PROJECT"
              value = var.task_project_id
            }

            resources {
              requests = merge({
                cpu                 = "${local.per_cpu_milli}m"
                memory              = "${local.per_memory_mib}Mi"
                "ephemeral-storage" = "${local.per_ephemeral}Mi"
              }, var.gpu != null && container.key == 0 ? { "nvidia.com/gpu" = tostring(var.gpu.count) } : {})
              limits = merge({
                cpu                 = "${local.per_cpu_milli}m"
                memory              = "${local.per_memory_mib}Mi"
                "ephemeral-storage" = "${local.per_ephemeral}Mi"
              }, var.gpu != null && container.key == 0 ? { "nvidia.com/gpu" = tostring(var.gpu.count) } : {})
            }

            volume_mount {
              name       = "data"
              mount_path = var.data_mount_path
            }

            dynamic "volume_mount" {
              for_each = var.hub_config_managed ? [1] : []
              content {
                name       = "hub-config-${container.value.name}"
                mount_path = var.hub_config_mount_path
                sub_path   = "jupyterhub_config.py"
                read_only  = true
              }
            }
            dynamic "volume_mount" {
              for_each = var.hub_config_managed ? [1] : []
              content {
                name       = "hub-oauth"
                mount_path = "/etc/jupyterhub/oauth"
                read_only  = true
              }
            }

            startup_probe {
              tcp_socket {
                port = container.value.hub_port
              }
              period_seconds    = 10
              failure_threshold = 60
            }
            readiness_probe {
              tcp_socket {
                port = container.value.hub_port
              }
              period_seconds = 10
            }
          }
        }

        volume {
          name = "data"
          persistent_volume_claim {
            claim_name = kubernetes_persistent_volume_claim.data.metadata[0].name
          }
        }
        dynamic "volume" {
          for_each = var.hub_config_managed ? local.ports : {}
          content {
            name = "hub-config-${volume.value.name}"
            config_map {
              name = kubernetes_config_map.hub_config[volume.key].metadata[0].name
            }
          }
        }
        dynamic "volume" {
          for_each = var.hub_config_managed ? [1] : []
          content {
            name = "hub-oauth"
            secret {
              secret_name = var.oauth_secret_name
            }
          }
        }
      }
    }
  }

  depends_on = [google_service_account_iam_member.workload_identity]
}

# ------------------------------------------------------------------ service
# One port per container. GKE creates one standalone NEG per port; the runner
# attaches each NEG to its backend service and adds the URL map rule.
resource "kubernetes_service" "jupyter" {
  metadata {
    name      = "jupyter"
    namespace = kubernetes_namespace.task.metadata[0].name
    labels    = local.labels
    annotations = {
      "cloud.google.com/neg" = jsonencode({
        exposed_ports = { for port, c in local.ports : port => {} }
      })
    }
  }
  spec {
    type     = "ClusterIP"
    selector = local.selector

    dynamic "port" {
      for_each = local.ports
      content {
        name        = port.value.name
        port        = port.value.hub_port
        target_port = port.value.hub_port
      }
    }
  }

  lifecycle {
    # Written by the NEG controller.
    ignore_changes = [metadata[0].annotations["cloud.google.com/neg-status"]]
  }
}

resource "kubernetes_resource_quota" "task" {
  metadata {
    name      = "task-quota"
    namespace = kubernetes_namespace.task.metadata[0].name
    labels    = local.labels
  }
  spec {
    hard = {
      "persistentvolumeclaims" = "1"
      "pods"                   = "2"
    }
  }
}

output "namespace" { value = var.namespace }
output "service_ports" { value = keys(local.ports) }
output "replicas" { value = var.running ? 1 : 0 }
