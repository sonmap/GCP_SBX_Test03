terraform {
  required_version = ">= 1.5.7, < 2.0.0"
  # No backend block: Infrastructure Manager keeps the state of each
  # deployment (<prefix>-<task>-<hash>-<stage>).
  required_providers {
    google = { source = "hashicorp/google", version = "~> 7.0" }
  }
}
