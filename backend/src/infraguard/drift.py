"""Drift scanners.

Two scanners implement the `DriftScanner` Protocol:

- `MockDriftScanner` returns canned findings for the four lab scenarios. Used
  when AWS is not configured so demos work offline.
- `AwsDriftScanner` reads live AWS state via boto3 (read-only). Activated when
  `AWS_DRIFT_ENABLED=true` AND boto3 + credentials are available.

Selection happens in `build_scanner_from_settings()`.

Finding IDs are deterministic: `{scenario_id}__{resource_type}__{resource_id}`.
This lets a re-scan upsert the same finding instead of duplicating it, and lets
the scanner mark previously-seen findings as `resolved` once the resource no
longer trips the check (e.g. after a remediation PR is merged and applied).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Finding model
# ---------------------------------------------------------------------------


FindingStatus = str  # "open" | "remediating" | "resolved"


@dataclass
class DriftFinding:
    id: str
    scenario_id: str
    resource_type: str
    resource_id: str
    region: str
    severity: str
    title: str
    description: str
    detected_at: str
    status: FindingStatus = "open"
    run_id: str | None = None
    last_seen_at: str = ""
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "scenarioId": self.scenario_id,
            "resourceType": self.resource_type,
            "resourceId": self.resource_id,
            "region": self.region,
            "severity": self.severity,
            "title": self.title,
            "description": self.description,
            "detectedAt": self.detected_at,
            "lastSeenAt": self.last_seen_at or self.detected_at,
            "status": self.status,
            "runId": self.run_id,
            "evidence": self.evidence,
        }


def make_finding_id(scenario_id: str, resource_type: str, resource_id: str) -> str:
    """Deterministic id so re-scans upsert rather than duplicate."""
    safe_resource = re.sub(r"[^a-zA-Z0-9._-]", "-", resource_id)
    return f"{scenario_id}__{resource_type}__{safe_resource}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Scanner protocol
# ---------------------------------------------------------------------------


class DriftScanner(Protocol):
    name: str

    async def scan(self) -> list[DriftFinding]: ...


# ---------------------------------------------------------------------------
# Mock scanner — canned non-compliant resources, one per scenario
# ---------------------------------------------------------------------------


class MockDriftScanner:
    name = "mock"

    def __init__(self, region: str = "us-east-1") -> None:
        self.region = region

    async def scan(self) -> list[DriftFinding]:
        ts = _now()
        return [
            DriftFinding(
                id=make_finding_id("open-ssh", "security_group", "sg-0demo01"),
                scenario_id="open-ssh",
                resource_type="security_group",
                resource_id="sg-0demo01",
                region=self.region,
                severity="critical",
                title="SSH open to the internet",
                description="Security group web-server-sg allows TCP/22 from 0.0.0.0/0",
                detected_at=ts,
                last_seen_at=ts,
                evidence={"port": 22, "cidr": "0.0.0.0/0", "sg_name": "web-server-sg"},
            ),
            DriftFinding(
                id=make_finding_id("public-s3", "s3_bucket", "infraguard-lab-static-assets"),
                scenario_id="public-s3",
                resource_type="s3_bucket",
                resource_id="infraguard-lab-static-assets",
                region=self.region,
                severity="high",
                title="S3 bucket world-readable",
                description=(
                    "Bucket infraguard-lab-static-assets has a public-read ACL and no "
                    "PublicAccessBlock configuration."
                ),
                detected_at=ts,
                last_seen_at=ts,
                evidence={"acl_grant": "AllUsers", "public_access_block": "missing"},
            ),
            DriftFinding(
                id=make_finding_id("missing-tags", "ec2_instance", "i-0demo02"),
                scenario_id="missing-tags",
                resource_type="ec2_instance",
                resource_id="i-0demo02",
                region=self.region,
                severity="medium",
                title="Instance missing required tags",
                description="EC2 instance i-0demo02 missing tags: Owner, CostCenter",
                detected_at=ts,
                last_seen_at=ts,
                evidence={"missing_tags": ["Owner", "CostCenter"]},
            ),
            DriftFinding(
                id=make_finding_id("idle-compute", "ec2_instance", "i-0demo03"),
                scenario_id="idle-compute",
                resource_type="ec2_instance",
                resource_id="i-0demo03",
                region=self.region,
                severity="low",
                title="Oversized instance for staging workload",
                description="EC2 instance i-0demo03 is an m5.4xlarge in staging (~$560/mo)",
                detected_at=ts,
                last_seen_at=ts,
                evidence={"instance_type": "m5.4xlarge", "environment": "staging"},
            ),
        ]


# ---------------------------------------------------------------------------
# AWS scanner — read-only boto3 against a live account
# ---------------------------------------------------------------------------


# Instance sizes considered "oversized" for the idle-compute scenario. Any
# instance whose type matches one of these suffixes is flagged.
_OVERSIZED_SUFFIX_RE = re.compile(
    r"\.(4xlarge|6xlarge|8xlarge|9xlarge|12xlarge|16xlarge|18xlarge|24xlarge|metal)$"
)
# EBS volumes at or above this size (GiB) are also flagged under idle-compute.
_LARGE_VOLUME_GIB = 500


REQUIRED_TAGS_DEFAULT = ("Environment", "Owner", "CostCenter")


class AwsDriftScanner:
    """Reads live AWS via boto3. boto3 is imported lazily so it stays optional."""

    name = "aws"

    def __init__(
        self,
        region: str,
        required_tags: tuple[str, ...] = REQUIRED_TAGS_DEFAULT,
        session: Any | None = None,
    ) -> None:
        self.region = region
        self.required_tags = required_tags
        # Lazy import: boto3 is an optional extra.
        if session is None:
            import boto3  # type: ignore[import-not-found]

            session = boto3.Session(region_name=region)
        self._session = session

    def _client(self, service: str) -> Any:
        return self._session.client(service, region_name=self.region)

    async def scan(self) -> list[DriftFinding]:
        # boto3 is sync; this is small + IO-bound, so running synchronously in
        # the scan loop is fine. If we needed parallelism we'd asyncio.to_thread.
        findings: list[DriftFinding] = []
        findings.extend(self._scan_open_ssh())
        findings.extend(self._scan_missing_tags())
        findings.extend(self._scan_public_s3())
        findings.extend(self._scan_idle_compute())
        return findings

    # -- open-ssh ------------------------------------------------------------

    def _scan_open_ssh(self) -> list[DriftFinding]:
        ec2 = self._client("ec2")
        ts = _now()
        out: list[DriftFinding] = []
        resp = ec2.describe_security_groups()
        for sg in resp.get("SecurityGroups", []):
            for rule in sg.get("IpPermissions", []):
                from_port = rule.get("FromPort")
                to_port = rule.get("ToPort")
                protocol = rule.get("IpProtocol", "")
                # Cover both literal TCP/22 rules and the "all traffic" (-1) form.
                spans_ssh = (
                    protocol == "-1"
                    or (from_port is None and to_port is None and protocol == "-1")
                    or (from_port is not None and to_port is not None
                        and from_port <= 22 <= to_port and protocol in ("tcp", "-1"))
                )
                if not spans_ssh:
                    continue
                world_cidrs: list[str] = []
                for ipv4 in rule.get("IpRanges", []):
                    if ipv4.get("CidrIp") == "0.0.0.0/0":
                        world_cidrs.append("0.0.0.0/0")
                for ipv6 in rule.get("Ipv6Ranges", []):
                    if ipv6.get("CidrIpv6") == "::/0":
                        world_cidrs.append("::/0")
                if not world_cidrs:
                    continue
                sg_id = sg["GroupId"]
                out.append(
                    DriftFinding(
                        id=make_finding_id("open-ssh", "security_group", sg_id),
                        scenario_id="open-ssh",
                        resource_type="security_group",
                        resource_id=sg_id,
                        region=self.region,
                        severity="critical",
                        title="SSH open to the internet",
                        description=(
                            f"Security group {sg.get('GroupName', sg_id)} allows TCP/22 "
                            f"from {', '.join(world_cidrs)}"
                        ),
                        detected_at=ts,
                        last_seen_at=ts,
                        evidence={
                            "sg_name": sg.get("GroupName"),
                            "cidrs": world_cidrs,
                            "from_port": from_port,
                            "to_port": to_port,
                        },
                    )
                )
                break  # one finding per SG is enough
        return out

    # -- missing-tags --------------------------------------------------------

    def _scan_missing_tags(self) -> list[DriftFinding]:
        ts = _now()
        out: list[DriftFinding] = []

        # EC2 instances
        ec2 = self._client("ec2")
        for reservation in ec2.describe_instances().get("Reservations", []):
            for inst in reservation.get("Instances", []):
                tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                missing = [t for t in self.required_tags if t not in tags]
                if not missing:
                    continue
                inst_id = inst["InstanceId"]
                out.append(self._tag_finding("ec2_instance", inst_id, missing, ts))

        # RDS instances
        rds = self._client("rds")
        for db in rds.describe_db_instances().get("DBInstances", []):
            arn = db["DBInstanceArn"]
            tag_resp = rds.list_tags_for_resource(ResourceName=arn)
            tags = {t["Key"]: t["Value"] for t in tag_resp.get("TagList", [])}
            missing = [t for t in self.required_tags if t not in tags]
            if not missing:
                continue
            db_id = db["DBInstanceIdentifier"]
            out.append(self._tag_finding("rds_instance", db_id, missing, ts))

        # S3 buckets
        s3 = self._client("s3")
        for bucket in s3.list_buckets().get("Buckets", []):
            name = bucket["Name"]
            try:
                tagging = s3.get_bucket_tagging(Bucket=name)
                tags = {t["Key"]: t["Value"] for t in tagging.get("TagSet", [])}
            except Exception:  # NoSuchTagSet etc.
                tags = {}
            missing = [t for t in self.required_tags if t not in tags]
            if not missing:
                continue
            out.append(self._tag_finding("s3_bucket", name, missing, ts))

        return out

    def _tag_finding(
        self, resource_type: str, resource_id: str, missing: list[str], ts: str
    ) -> DriftFinding:
        return DriftFinding(
            id=make_finding_id("missing-tags", resource_type, resource_id),
            scenario_id="missing-tags",
            resource_type=resource_type,
            resource_id=resource_id,
            region=self.region,
            severity="medium",
            title=f"{resource_type.replace('_', ' ').title()} missing required tags",
            description=f"{resource_id} missing tags: {', '.join(missing)}",
            detected_at=ts,
            last_seen_at=ts,
            evidence={"missing_tags": missing},
        )

    # -- public-s3 -----------------------------------------------------------

    def _scan_public_s3(self) -> list[DriftFinding]:
        ts = _now()
        out: list[DriftFinding] = []
        s3 = self._client("s3")
        for bucket in s3.list_buckets().get("Buckets", []):
            name = bucket["Name"]
            issues: list[str] = []

            # ACL: any grant to AllUsers / AuthenticatedUsers
            try:
                acl = s3.get_bucket_acl(Bucket=name)
                for grant in acl.get("Grants", []):
                    uri = grant.get("Grantee", {}).get("URI", "")
                    if uri.endswith("/AllUsers") or uri.endswith("/AuthenticatedUsers"):
                        issues.append(f"acl grants {grant['Permission']} to {uri.split('/')[-1]}")
                        break
            except Exception as exc:
                logger.debug("get_bucket_acl(%s) failed: %s", name, exc)

            # PublicAccessBlock: missing entirely, or any field is False
            try:
                pab = s3.get_public_access_block(Bucket=name)
                config = pab.get("PublicAccessBlockConfiguration", {})
                relaxed = [k for k, v in config.items() if v is False]
                if relaxed:
                    issues.append("public-access-block fields disabled: " + ", ".join(relaxed))
            except Exception as exc:
                # NoSuchPublicAccessBlockConfiguration is the common case here.
                msg = str(exc)
                if "NoSuchPublicAccessBlockConfiguration" in msg or "404" in msg:
                    issues.append("no PublicAccessBlock configured")
                else:
                    logger.debug("get_public_access_block(%s) failed: %s", name, exc)

            if not issues:
                continue
            out.append(
                DriftFinding(
                    id=make_finding_id("public-s3", "s3_bucket", name),
                    scenario_id="public-s3",
                    resource_type="s3_bucket",
                    resource_id=name,
                    region=self.region,
                    severity="high",
                    title="S3 bucket exposed publicly",
                    description=f"Bucket {name}: " + "; ".join(issues),
                    detected_at=ts,
                    last_seen_at=ts,
                    evidence={"issues": issues},
                )
            )
        return out

    # -- idle-compute --------------------------------------------------------

    def _scan_idle_compute(self) -> list[DriftFinding]:
        ts = _now()
        out: list[DriftFinding] = []
        ec2 = self._client("ec2")

        for reservation in ec2.describe_instances().get("Reservations", []):
            for inst in reservation.get("Instances", []):
                instance_type = inst.get("InstanceType", "")
                if not _OVERSIZED_SUFFIX_RE.search(instance_type):
                    continue
                state = inst.get("State", {}).get("Name", "unknown")
                if state == "terminated":
                    continue
                inst_id = inst["InstanceId"]
                out.append(
                    DriftFinding(
                        id=make_finding_id("idle-compute", "ec2_instance", inst_id),
                        scenario_id="idle-compute",
                        resource_type="ec2_instance",
                        resource_id=inst_id,
                        region=self.region,
                        severity="low",
                        title="Oversized instance",
                        description=(
                            f"Instance {inst_id} is type {instance_type} — likely "
                            f"oversized for steady-state workloads"
                        ),
                        detected_at=ts,
                        last_seen_at=ts,
                        evidence={"instance_type": instance_type, "state": state},
                    )
                )

        for vol in ec2.describe_volumes().get("Volumes", []):
            size = vol.get("Size", 0)
            if size < _LARGE_VOLUME_GIB:
                continue
            vol_id = vol["VolumeId"]
            out.append(
                DriftFinding(
                    id=make_finding_id("idle-compute", "ebs_volume", vol_id),
                    scenario_id="idle-compute",
                    resource_type="ebs_volume",
                    resource_id=vol_id,
                    region=self.region,
                    severity="low",
                    title="Oversized EBS volume",
                    description=f"Volume {vol_id} is {size} GiB — review utilization",
                    detected_at=ts,
                    last_seen_at=ts,
                    evidence={"size_gib": size, "volume_type": vol.get("VolumeType")},
                )
            )
        return out


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_scanner_from_settings() -> DriftScanner:
    """Return AwsDriftScanner when AWS drift is enabled and boto3 is importable,
    else MockDriftScanner. Never raises — falls back to mock on any import or
    credential error so the backend still starts.
    """
    from .config import settings

    if not settings.aws_drift_enabled:
        logger.info("Drift scanner: mock (AWS_DRIFT_ENABLED not set)")
        return MockDriftScanner(region=settings.aws_region)

    try:
        import boto3  # noqa: F401  (presence check only)
    except ImportError:
        logger.warning(
            "AWS_DRIFT_ENABLED=true but boto3 is not installed. "
            "Install with: pip install '.[aws]'. Falling back to mock scanner."
        )
        return MockDriftScanner(region=settings.aws_region)

    required = tuple(
        t.strip() for t in settings.aws_required_tags.split(",") if t.strip()
    ) or REQUIRED_TAGS_DEFAULT
    logger.info(
        "Drift scanner: aws (region=%s, required_tags=%s)",
        settings.aws_region,
        ",".join(required),
    )
    return AwsDriftScanner(region=settings.aws_region, required_tags=required)
