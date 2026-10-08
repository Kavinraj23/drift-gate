terraform {
  required_providers {
    null = {
      source  = "hashicorp/null"
      version = "~> 9.0"
    }
  }
}

resource "null_resource" "canary" {}
