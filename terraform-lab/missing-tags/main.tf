# INTENTIONALLY NON-COMPLIANT — InfraGuard test scenario
# Issue: Resources missing required tags (Environment, Owner, CostCenter)
# Expected fix: Add required tags to all resources

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

# VIOLATION: No required tags
resource "aws_instance" "app_server" {
  ami           = "ami-0c55b159cbfafe1f0"
  instance_type = "t3.medium"

  root_block_device {
    volume_size = 20
    volume_type = "gp3"
  }
}

# VIOLATION: No required tags
resource "aws_db_instance" "database" {
  allocated_storage    = 20
  storage_type         = "gp3"
  engine               = "postgres"
  engine_version       = "15.4"
  instance_class       = "db.t3.micro"
  db_name              = "appdb"
  username             = "admin"
  password             = "temporary-lab-password"
  parameter_group_name = "default.postgres15"
  skip_final_snapshot  = true
}

# VIOLATION: No required tags
resource "aws_s3_bucket" "app_data" {
  bucket = "infraguard-lab-app-data"
}
