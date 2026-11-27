terraform {
  # Infrastructure Manager runs Terraform 1.5.7.
  required_version = ">= 1.5.7, < 2.0.0"

  # No backend block: Infrastructure Manager keeps the state of this
  # deployment (<prefix>-<task>-<hash>-workload).
  required_providers {
    google     = { source = "hashicorp/google", version = "~> 7.0" }
    kubernetes = { source = "hashicorp/kubernetes", version = "~> 2.35" }
  }
}

provider "google" {
  project = var.gke_project_id
}

# Identity that may edit the IAM policy of the task's service account.
# Empty = the deployment's service account (tf-manager).
provider "google" {
  alias                       = "task"
  project                     = var.task_project_id
  impersonate_service_account = var.wi_impersonate_service_account == "" ? null : var.wi_impersonate_service_account
}

# ------------------------------------------------------------------ cluster access
# Infrastructure Manager has no kubeconfig. The provider talks to the cluster
# with the deployment service account's token.
#   dns      : DNS-based endpoint (gke-....gke.goog), Google-signed certificate
#   internal : private IP endpoint, cluster CA; needs a route from the worker pool
data "google_client_config" "current" {}

data "google_container_cluster" "sandbox" {
  name     = var.cluster_name
  location = var.cluster_location
  project  = var.gke_project_id
}

locals {
  cluster_host = var.gke_endpoint_mode == "dns" ? "https://${data.google_container_cluster.sandbox.control_plane_endpoints_config[0].dns_endpoint_config[0].endpoint}" : "https://${data.google_container_cluster.sandbox.private_cluster_config[0].private_endpoint}"
  cluster_ca   = var.gke_endpoint_mode == "dns" ? null : base64decode(data.google_container_cluster.sandbox.master_auth[0].cluster_ca_certificate)
}

provider "kubernetes" {
  host                   = local.cluster_host
  token                  = data.google_client_config.current.access_token
  cluster_ca_certificate = local.cluster_ca
}
