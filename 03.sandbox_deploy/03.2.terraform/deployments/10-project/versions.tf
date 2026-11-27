terraform {
  required_version = ">= 1.9.0, < 2.0.0"
  # Same place for every task and stage. The runner supplies:
  #   -backend-config=bucket=<TF_STATE_BUCKET>
  #   -backend-config=prefix=tasks/<task>/<stage>
  backend "gcs" {}

  required_providers {
    google = { source = "hashicorp/google", version = "~> 7.0" }
  }
}
