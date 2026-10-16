terraform {
  required_version = ">= 1.5, < 2.0"
  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.36"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.17"
    }
  }
}

variable "kube_config_path" {
  type    = string
  default = "~/.kube/config"
}
variable "kube_context" {
  type    = string
  default = "k3d-skillpulse"
}
variable "image_repository" {
  type    = string
  default = "k3d-skillpulse-registry:5000/skillpulse"
}
variable "image_tag" {
  type    = string
  default = "dev"
}
variable "values_file" {
  type    = string
  default = "values-local.yaml"
  validation {
    condition     = contains(["values-local.yaml", "values-ci.yaml"], var.values_file)
    error_message = "Use the local or CI values file."
  }
}

provider "kubernetes" {
  config_path    = pathexpand(var.kube_config_path)
  config_context = var.kube_context
}
provider "helm" {
  kubernetes {
    config_path    = pathexpand(var.kube_config_path)
    config_context = var.kube_context
  }
}

resource "kubernetes_namespace" "skillpulse" {
  metadata {
    name = "skillpulse"
  }
}

resource "helm_release" "cnpg" {
  name       = "cnpg"
  repository = "https://cloudnative-pg.github.io/charts"
  chart      = "cloudnative-pg"
  version    = "0.23.2"
  namespace  = kubernetes_namespace.skillpulse.metadata[0].name
  wait       = true
  timeout    = 300
}

# Apply the operator first: the Kubernetes provider needs its CRD during planning.
resource "kubernetes_manifest" "cnpg_cluster" {
  depends_on = [helm_release.cnpg]
  manifest = {
    apiVersion = "postgresql.cnpg.io/v1"
    kind       = "Cluster"
    metadata = {
      name      = "skillpulse-db"
      namespace = kubernetes_namespace.skillpulse.metadata[0].name
    }
    spec = {
      instances = 1
      imageName = "ghcr.io/cloudnative-pg/postgresql:17.4"
      bootstrap = {
        initdb = {
          database = "skillpulse"
          owner    = "skillpulse"
          secret   = { name = "skillpulse-db-owner" }
        }
      }
      # Migration 0003 creates the NOLOGIN, SELECT-only tools role.
      managed = {
        roles = [{
          name           = "skillpulse"
          ensure         = "present"
          login          = true
          createrole     = true
          passwordSecret = { name = "skillpulse-db-owner" }
          }, {
          name           = "skillpulse_api"
          ensure         = "present"
          login          = true
          superuser      = false
          createrole     = false
          createdb       = false
          passwordSecret = { name = "skillpulse-db-api" }
          }, {
          name           = "skillpulse_mcp"
          ensure         = "present"
          login          = true
          superuser      = false
          createrole     = false
          createdb       = false
          passwordSecret = { name = "skillpulse-db-mcp" }
        }]
      }
      storage = { size = "1Gi", storageClass = "local-path" }
      resources = {
        requests = { cpu = "100m", memory = "256Mi" }
        limits   = { cpu = "1", memory = "512Mi" }
      }
    }
  }
}

locals {
  redis_labels = { "app.kubernetes.io/name" = "skillpulse-redis" }
}
resource "kubernetes_deployment" "redis" {
  metadata {
    name      = "skillpulse-redis"
    namespace = kubernetes_namespace.skillpulse.metadata[0].name
  }
  spec {
    replicas = 1
    selector {
      match_labels = local.redis_labels
    }
    template {
      metadata {
        labels = local.redis_labels
      }
      spec {
        security_context {
          run_as_non_root = true
          run_as_user     = 999
          run_as_group    = 999
          fs_group        = 999
        }
        container {
          name    = "redis"
          image   = "redis:7.4-alpine"
          command = ["redis-server", "--save", "", "--appendonly", "no"]
          port {
            container_port = 6379
          }
          security_context {
            allow_privilege_escalation = false
            read_only_root_filesystem  = true
            capabilities {
              drop = ["ALL"]
            }
          }
          resources {
            requests = { cpu = "25m", memory = "64Mi" }
            limits   = { cpu = "250m", memory = "128Mi" }
          }
          readiness_probe {
            exec {
              command = ["redis-cli", "ping"]
            }
          }
          liveness_probe {
            exec {
              command = ["redis-cli", "ping"]
            }
            initial_delay_seconds = 10
          }
        }
      }
    }
  }
}
resource "kubernetes_service" "redis" {
  metadata {
    name      = "skillpulse-redis"
    namespace = kubernetes_namespace.skillpulse.metadata[0].name
  }
  spec {
    selector = local.redis_labels
    port {
      port        = 6379
      target_port = "6379"
    }
  }
}

resource "helm_release" "skillpulse" {
  depends_on = [kubernetes_manifest.cnpg_cluster, kubernetes_service.redis]
  name       = "skillpulse"
  chart      = "${path.module}/../deploy/helm/skillpulse"
  namespace  = kubernetes_namespace.skillpulse.metadata[0].name
  values     = [file("${path.module}/../deploy/helm/skillpulse/${var.values_file}")]
  wait       = true
  atomic     = true
  timeout    = 600
  set {
    name  = "image.repository"
    value = var.image_repository
  }
  set {
    name  = "image.tag"
    value = var.image_tag
  }
}
