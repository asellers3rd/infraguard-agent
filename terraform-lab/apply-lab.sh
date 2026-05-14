#!/usr/bin/env bash
# Apply (or destroy) the three free-tier-friendly InfraGuard lab scenarios.
#
# Usage:
#   AWS_PROFILE=infraguard-tf ./apply-lab.sh           # apply
#   AWS_PROFILE=infraguard-tf ./apply-lab.sh destroy   # tear down
#
# Skips: idle-compute (cost), aws_db_instance.database inside missing-tags
# (RDS Postgres is not always free). If you hit BucketAlreadyExists on S3,
# edit the `bucket = "..."` line in the affected scenario's main.tf.

set -euo pipefail

ACTION="${1:-apply}"
SCENARIOS=("open-ssh" "missing-tags" "public-s3")
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v terraform >/dev/null; then
  echo "terraform not on PATH — install it first" >&2
  exit 1
fi

if ! aws sts get-caller-identity >/dev/null 2>&1; then
  echo "AWS creds not configured (try: aws configure --profile infraguard-tf)" >&2
  exit 1
fi

# missing-tags: only the EC2 + S3 resources, not the RDS instance. Using
# -target keeps the canonical .tf file untouched while avoiding the RDS spin-up
# (~$15/mo outside the new-account free tier).
MISSING_TAGS_TARGETS=(
  -target=aws_instance.app_server
  -target=aws_s3_bucket.app_data
)

for scenario in "${SCENARIOS[@]}"; do
  echo
  echo "=== ${ACTION} ${scenario} ==="
  cd "${SCRIPT_DIR}/${scenario}"
  terraform init -upgrade -input=false

  case "${ACTION}" in
    apply)
      if [[ "${scenario}" == "missing-tags" ]]; then
        terraform apply -auto-approve -input=false "${MISSING_TAGS_TARGETS[@]}"
      else
        terraform apply -auto-approve -input=false
      fi
      ;;
    destroy)
      terraform destroy -auto-approve -input=false
      ;;
    *)
      echo "Unknown action: ${ACTION} (expected: apply | destroy)" >&2
      exit 1
      ;;
  esac
done

echo
echo "Done. Verify with:"
echo "  aws ec2 describe-security-groups --filters Name=group-name,Values=web-server-sg"
echo "  aws s3 ls | grep infraguard-lab"
