terraform {
  required_version = ">= 1.9.0, < 2.0.0"
  required_providers {
    google     = { source = "hashicorp/google", version = "~> 7.0" }
    kubernetes = { source = "hashicorp/kubernetes", version = "~> 2.35" }
  }

  # One state per task. The runner supplies:
  #   -backend-config=bucket=<TF_STATE_BUCKET>
  #   -backend-config=prefix=tasks/<task>/50-workload
  backend "gcs" {}
}

# Kubernetes access uses the kubeconfig written by the runner
# (gcloud container clusters get-credentials, DNS or internal endpoint).
provider "kubernetes" {
  config_path = var.kubeconfig_path
}

provider "google" {
  project = var.gke_project_id
}

# Identity that may edit the IAM policy of the task's service account.
# Empty = same identity as the runner.
provider "google" {
  alias                       = "task"
  project                     = var.task_project_id
  impersonate_service_account = var.wi_impersonate_service_account == "" ? null : var.wi_impersonate_service_account
}
