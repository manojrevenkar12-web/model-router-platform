variable "environment" {
  type        = string
  description = "staging or production"
  validation {
    condition     = contains(["staging", "production"], var.environment)
    error_message = "environment must be staging or production"
  }
}

variable "region" {
  type    = string
  default = "fr-par"
}

variable "zone" {
  type    = string
  default = "fr-par-2" # L4 GPUs
}

variable "k8s_version" {
  type    = string
  default = "1.31"
}

variable "app_node_type" {
  type    = string
  default = "PRO2-S"
}

variable "gpu_node_type" {
  type    = string
  default = "L4-1-24G"
}

variable "gpu_min_nodes" {
  type    = number
  default = 1
}

variable "gpu_max_nodes" {
  type    = number
  default = 4
}

variable "db_node_type" {
  type    = string
  default = "DB-PRO2-XXS"
}

variable "redis_node_type" {
  type    = string
  default = "RED1-S"
}

variable "redis_version" {
  type    = string
  default = "7.2.5"
}
