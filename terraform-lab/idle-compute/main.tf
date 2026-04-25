# INTENTIONALLY WASTEFUL — InfraGuard test scenario
# Issue: Oversized instance running 24/7 with no auto-scaling or scheduling
# Expected fix: Right-size instance, add auto-scaling group or scheduling

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

data "aws_ami" "amazon_linux" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-*-x86_64"]
  }
}

# VIOLATION: Oversized instance type for a dev/staging workload
# m5.4xlarge = 16 vCPU, 64 GB RAM — ~$560/month on-demand
resource "aws_instance" "batch_processor" {
  ami           = data.aws_ami.amazon_linux.id
  instance_type = "m5.4xlarge"

  root_block_device {
    volume_size = 200
    volume_type = "gp3"
  }

  # VIOLATION: No auto-scaling, no scheduling, always running
  # No associated launch template or ASG

  tags = {
    Name        = "batch-processor"
    Environment = "staging"
    ManagedBy   = "terraform"
  }
}

# VIOLATION: Large EBS volume provisioned but likely underutilized
resource "aws_ebs_volume" "data_volume" {
  availability_zone = "${var.aws_region}a"
  size              = 500
  type              = "gp3"
  iops              = 3000
  throughput        = 125

  tags = {
    Name        = "batch-data-volume"
    Environment = "staging"
    ManagedBy   = "terraform"
  }
}
