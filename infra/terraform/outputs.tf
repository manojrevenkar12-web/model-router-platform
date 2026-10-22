output "cluster_id" {
  value = scaleway_k8s_cluster.main.id
}

output "registry" {
  value = scaleway_registry_namespace.main.endpoint
}

output "kek_id" {
  value       = scaleway_key_manager_key.kek.id
  description = "Key Manager key wrapping customers' data keys"
}

output "database_secret_id" {
  value = scaleway_secret.db.id
}

output "redis_secret_id" {
  value = scaleway_secret.redis.id
}
