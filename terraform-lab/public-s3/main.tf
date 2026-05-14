# INTENTIONALLY INSECURE — InfraGuard test scenario
# Issue: S3 bucket has no aws_s3_bucket_public_access_block resource, leaving
#   it dependent on AWS's account/bucket defaults rather than explicit IaC.
# Expected fix: Add an aws_s3_bucket_public_access_block with all four
#   block_* fields set to true.

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

resource "aws_s3_bucket" "static_assets" {
  bucket = "infraguard-lab-static-assets"

  tags = {
    Name        = "static-assets"
    Environment = "lab"
    ManagedBy   = "terraform"
  }
}

# VIOLATION: No aws_s3_bucket_public_access_block resource is defined for this
# bucket. Without explicit IaC controls, public exposure is gated only by AWS
# account/bucket defaults — which can drift if a future change disables them.

resource "aws_s3_bucket_versioning" "static_assets_versioning" {
  bucket = aws_s3_bucket.static_assets.id

  versioning_configuration {
    status = "Enabled"
  }
}
