# INTENTIONALLY INSECURE — InfraGuard test scenario
# Issue: S3 bucket has public access enabled and no block_public_access
# Expected fix: Enable block_public_access, remove public ACL

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

# VIOLATION: Public ACL on bucket
resource "aws_s3_bucket" "static_assets" {
  bucket = "infraguard-lab-static-assets"

  tags = {
    Name        = "static-assets"
    Environment = "lab"
    ManagedBy   = "terraform"
  }
}

# VIOLATION: Public read ACL
resource "aws_s3_bucket_acl" "static_assets_acl" {
  bucket = aws_s3_bucket.static_assets.id
  acl    = "public-read"
}

# VIOLATION: No public access block configured
# aws_s3_bucket_public_access_block is missing entirely

resource "aws_s3_bucket_versioning" "static_assets_versioning" {
  bucket = aws_s3_bucket.static_assets.id

  versioning_configuration {
    status = "Enabled"
  }
}
