# AWS Setup — Activating the Drift Scanner

This guide turns the drift panel from *mocked* into *live integration*: provision a
sandbox AWS account, apply the lab Terraform scenarios, give the backend
read-only AWS creds, and watch real findings stream into the dashboard.

Estimated cost with the three free-tier scenarios in this guide: **a few cents
per day**, dominated by EBS storage on a single `t3.micro`. `idle-compute` is
deliberately excluded because it provisions an `m5.4xlarge` (~$0.77/hr).

## 1. Create a sandbox AWS account

Use a dedicated account, not a shared one. Two paths:

- **Standalone account:** sign up at <https://aws.amazon.com>. New accounts get
  12 months of free tier on `t3.micro`, S3, RDS `db.t3.micro`, etc.
- **AWS Organizations sub-account:** if you already have an Organization, create
  a new "infraguard-sandbox" account in it. Cleanest blast-radius isolation.

Either way, set a billing alert at $5/mo before going further:
Billing → Budgets → Create budget → Zero-spend / fixed amount → email alerts.

## 2. Create two IAM users

We separate the **terraform-apply** identity (broad create/delete) from the
**drift-scanner** identity (read-only). The scanner creds are what the backend
loads from `.env`; even if those leak, the blast radius is "describe AWS."

### 2a. Terraform apply user (`infraguard-tf`)

Console: IAM → Users → Create user → name `infraguard-tf` → "Provide user access
to the AWS Management Console" off → Next → "Attach policies directly" → for a
sandbox account it's fine to attach `PowerUserAccess` (excludes IAM admin).
Then: user → Security credentials → Create access key → CLI → download the key.

Configure locally:

```bash
aws configure --profile infraguard-tf
# paste the access key id, secret, region = us-east-1, output = json
```

### 2b. Drift scanner user (`infraguard-scanner`)

Same flow, but with a custom inline policy (least-privilege — only the API
calls `AwsDriftScanner` makes in `backend/src/infraguard/drift.py`):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ScannerReadOnly",
      "Effect": "Allow",
      "Action": [
        "ec2:DescribeSecurityGroups",
        "ec2:DescribeInstances",
        "ec2:DescribeVolumes",
        "rds:DescribeDBInstances",
        "rds:ListTagsForResource",
        "s3:ListAllMyBuckets",
        "s3:GetBucketTagging",
        "s3:GetBucketAcl",
        "s3:GetBucketPublicAccessBlock"
      ],
      "Resource": "*"
    }
  ]
}
```

Create access key → save the values for step 4.

## 3. Apply the lab Terraform

From the repo root:

```bash
cd infraguard-agent/terraform-lab
AWS_PROFILE=infraguard-tf ./apply-lab.sh
```

The script applies three scenarios (`open-ssh`, `missing-tags`, `public-s3`)
and skips the RDS resource inside `missing-tags` to keep cost near zero.
S3 bucket names are globally unique — if you get a `BucketAlreadyExists`
error, edit the `bucket = "..."` line in the affected scenario's `main.tf`
and re-run.

## 4. Wire the backend to live AWS

Append to `infraguard-agent/backend/.env`:

```
AWS_DRIFT_ENABLED=true
AWS_REGION=us-east-1
AWS_ACCESS_KEY_ID=<scanner-user-key>
AWS_SECRET_ACCESS_KEY=<scanner-user-secret>
```

Install the boto3 extra (one-time):

```bash
cd infraguard-agent/backend
./.venv/bin/pip install -e ".[aws]"
```

Start the backend:

```bash
./.venv/bin/uvicorn infraguard.main:app --reload
```

Within `AWS_DRIFT_SCAN_INTERVAL_SECONDS` (default 300s; lower it via env for
faster iteration), `GET /drift` returns real findings. The portfolio dashboard
in Live Mode renders them in the `DriftFindings` panel.

## 5. Run an end-to-end remediation

1. Open the portfolio at <http://localhost:3000/infraguard>.
2. Flip Mode to **Live**. Wait for a drift card to appear (e.g. "SSH open to the
   internet" on the open-ssh security group).
3. Click **Remediate**. The agent runs against `infraguard-lab`, opens a PR,
   CI runs (terraform + trivy + infracost). Approve in the dashboard.

## 6. Tear down

```bash
cd infraguard-agent/terraform-lab
AWS_PROFILE=infraguard-tf ./apply-lab.sh destroy
```

Then delete the two IAM users' access keys (or rotate them) and the users
themselves if the demo is done.
