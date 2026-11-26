# ---------------------------------------------------------------- platform
variable "kubeconfig_path" { type = string }
variable "gke_project_id" { type = string }

variable "wi_impersonate_service_account" {
  type    = string
  default = ""
}

# ---------------------------------------------------------------- task
variable "task_name" { type = string }
variable "task_project_id" { type = string }
variable "area" { type = string }
variable "namespace" { type = string }
variable "origin" { type = string }
variable "request_id" { type = string }

# Full email of the task project's service account used by the Pod.
variable "jupyter_gsa_email" { type = string }

variable "ksa_name" { type = string }

# ---------------------------------------------------------------- sizing
variable "running" {
  type        = bool
  description = "false = replicas 0. Disk, Service and routing stay."
}

variable "cpu" { type = number }
variable "memory_gib" { type = number }

variable "ephemeral_storage_gib" {
  type    = number
  default = 10
}

variable "compute_class" {
  type    = string
  default = "Balanced"
}

variable "gpu" {
  type    = object({ type = string, count = number })
  default = null
}

# ---------------------------------------------------------------- storage
variable "disk_size_gb" { type = number }
variable "zone" { type = string }

variable "existing_disk" {
  type        = string
  default     = ""
  description = "Name of a PD in the GKE project (restored from the VM data-disk snapshot). Empty = new disk."
}

variable "storage_class" {
  type        = string
  description = "Dynamic class for new disks, or the static class shared by PV and PVC for existing disks."
}

variable "data_mount_path" {
  type    = string
  default = "/home/jupyter"
}

# ---------------------------------------------------------------- containers
variable "containers" {
  type = list(object({
    name      = string
    image     = string
    hub_port  = number
    host      = string
    base_path = string
    command   = optional(list(string), [])
    args      = optional(list(string), [])
  }))
  validation {
    condition     = length(var.containers) >= 1
    error_message = "At least one container is required."
  }
}

# ---------------------------------------------------------------- hub config
variable "hub_config_managed" {
  type        = bool
  description = "true = mount the rendered jupyterhub_config.py over the image's own file."
}

variable "hub_config_mount_path" {
  type    = string
  default = "/etc/jupyterhub/jupyterhub_config.py"
}

variable "hub_oauth_base_url" {
  type    = string
  default = ""
}

variable "portal_members_api" {
  type    = string
  default = ""
}

variable "hub_admin_users" {
  type    = list(string)
  default = []
}

# K8s Secret holding client_id / client_secret. Created by the runner from
# Secret Manager so the values never enter Terraform state.
variable "oauth_secret_name" {
  type    = string
  default = "hub-oauth"
}
