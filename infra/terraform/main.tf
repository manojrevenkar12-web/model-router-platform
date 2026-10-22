# European infrastructure on Scaleway (fr-par): Kubernetes with a CPU pool for the gateway and
# workers and an autoscaled L4 GPU pool for open-weight model serving; managed PostgreSQL (HA,
# encrypted, private network only); managed Redis (TLS, private network only); Key Manager for the
# key-encryption key; Secret Manager for credentials. Nothing data-bearing has a public endpoint.

terraform {
  required_version = ">= 1.6"
  required_providers {
    scaleway = {
      source  = "scaleway/scaleway"
      version = "~> 2.50"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
  # State in Scaleway Object Storage, configured at `terraform init -backend-config=...`.
  backend "s3" {}
}

provider "scaleway" {
  region = var.region
  zone   = var.zone
}

locals {
  name = "mrp-${var.environment}"
  tags = ["mrp", var.environment, "managed-by-terraform"]
}

resource "scaleway_vpc" "main" {
  name = local.name
  tags = local.tags
}

resource "scaleway_vpc_private_network" "main" {
  name   = local.name
  vpc_id = scaleway_vpc.main.id
  tags   = local.tags
}

# ---- Kubernetes ---------------------------------------------------------------------------------

resource "scaleway_k8s_cluster" "main" {
  name                        = local.name
  version                     = var.k8s_version
  cni                         = "cilium"
  private_network_id          = scaleway_vpc_private_network.main.id
  delete_additional_resources = false
  tags                        = local.tags

  auto_upgrade {
    enable                        = true
    maintenance_window_start_hour = 3
    maintenance_window_day        = "sunday"
  }
}

resource "scaleway_k8s_pool" "app" {
  cluster_id  = scaleway_k8s_cluster.main.id
  name        = "app"
  node_type   = var.app_node_type
  size        = 3
  min_size    = 3
  max_size    = 8
  autoscaling = true
  autohealing = true
  tags        = local.tags
}

# GPUs for open-weight models (vLLM). Scales to zero outside serving hours if configured.
resource "scaleway_k8s_pool" "gpu" {
  cluster_id  = scaleway_k8s_cluster.main.id
  name        = "gpu-l4"
  node_type   = var.gpu_node_type
  size        = var.gpu_min_nodes
  min_size    = var.gpu_min_nodes
  max_size    = var.gpu_max_nodes
  autoscaling = true
  autohealing = true
  tags        = concat(local.tags, ["gpu"])
}

# ---- Data -----------------------------------------------------------------------------------------

resource "random_password" "db" {
  length  = 32
  special = false
}

resource "random_password" "redis" {
  length  = 32
  special = true
}

resource "scaleway_rdb_instance" "pg" {
  name                      = local.name
  node_type                 = var.db_node_type
  engine                    = "PostgreSQL-16"
  is_ha_cluster             = true
  disable_backup            = false
  backup_schedule_frequency = 24
  backup_schedule_retention = 14
  encryption_at_rest        = true
  user_name                 = "mrp_owner"
  password                  = random_password.db.result
  tags                      = local.tags

  private_network {
    pn_id       = scaleway_vpc_private_network.main.id
    enable_ipam = true
  }
}

resource "scaleway_redis_cluster" "main" {
  name         = local.name
  version      = var.redis_version
  node_type    = var.redis_node_type
  user_name    = "mrp"
  password     = random_password.redis.result
  cluster_size = 2
  tls_enabled  = true
  tags         = local.tags

  private_networks {
    id = scaleway_vpc_private_network.main.id
  }
}

# ---- Keys and secrets ---------------------------------------------------------------------------

# The key-encryption key for customers' provider keys. It never leaves Key Manager: the gateway asks
# Key Manager to wrap and unwrap data keys. Rotated every 90 days; old versions stay for unwrapping
# until `POST /admin/kek/rotate` has re-wrapped every data key.
resource "scaleway_keymanager_key" "kek" {
  name        = "${local.name}-provider-keys"
  usage       = "symmetric_encryption"
  algorithm   = "aes_256_gcm"
  description = "Wraps the data keys that seal customers' provider API keys"
  tags        = local.tags
  unprotected = false

  rotation_policy {
    rotation_period = "2160h"
  }
}

resource "scaleway_secret" "db" {
  name        = "${local.name}-database-url"
  description = "PostgreSQL connection for the schema owner (migrations, reconciliation)"
  tags        = local.tags
}

resource "scaleway_secret_version" "db" {
  secret_id = scaleway_secret.db.id
  data      = "postgresql://mrp_owner:${random_password.db.result}@${scaleway_rdb_instance.pg.private_network[0].ip}:${scaleway_rdb_instance.pg.private_network[0].port}/rdb?sslmode=require"
}

resource "scaleway_secret" "redis" {
  name        = "${local.name}-redis-url"
  description = "Redis connection (TLS)"
  tags        = local.tags
}

resource "scaleway_secret_version" "redis" {
  secret_id = scaleway_secret.redis.id
  data      = "rediss://mrp:${urlencode(random_password.redis.result)}@${scaleway_redis_cluster.main.private_networks[0].ips[0]}:${scaleway_redis_cluster.main.private_networks[0].port}/0"
}

resource "scaleway_registry_namespace" "main" {
  name      = local.name
  is_public = false
}
