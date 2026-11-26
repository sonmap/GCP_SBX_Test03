variable "project_id" { type = string }
variable "region" { type = string }
variable "task_name" { type = string }
variable "group_email" { type = string }
variable "bigquery_dataset" { type = string }
variable "gcs_bucket" { type = string }

# Service account of the task project used by the Pod (full email).
variable "jupyter_gsa_email" { type = string }

variable "create_jupyter_gsa" {
  type    = bool
  default = false
}

variable "datalake_datasets" {
  type    = list(object({ project_id = string, dataset_id = string }))
  default = []
}
