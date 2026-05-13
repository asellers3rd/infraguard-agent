"""Tests for drift scanners + store + drift routes."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from infraguard.drift import (
    AwsDriftScanner,
    DriftFinding,
    MockDriftScanner,
    build_scanner_from_settings,
    make_finding_id,
)
from infraguard.main import app
from infraguard.routes import reset_drift_scanner
from infraguard.store import DriftStore


# ---------------------------------------------------------------------------
# Stable IDs
# ---------------------------------------------------------------------------


def test_finding_id_is_deterministic():
    a = make_finding_id("open-ssh", "security_group", "sg-abc")
    b = make_finding_id("open-ssh", "security_group", "sg-abc")
    assert a == b == "open-ssh__security_group__sg-abc"


def test_finding_id_sanitizes_special_chars():
    # Bucket names with dots / slashes should still produce a valid id.
    fid = make_finding_id("public-s3", "s3_bucket", "my.bucket/with slashes")
    assert "/" not in fid
    assert " " not in fid


# ---------------------------------------------------------------------------
# MockDriftScanner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mock_scanner_returns_one_finding_per_scenario():
    scanner = MockDriftScanner(region="us-east-1")
    findings = await scanner.scan()
    by_scenario = {f.scenario_id for f in findings}
    assert by_scenario == {"open-ssh", "missing-tags", "public-s3", "idle-compute"}
    for f in findings:
        assert f.status == "open"
        assert f.region == "us-east-1"
        assert f.id.startswith(f.scenario_id + "__")


# ---------------------------------------------------------------------------
# DriftStore upsert behavior
# ---------------------------------------------------------------------------


def _finding(scenario: str, resource: str, *, status: str = "open") -> DriftFinding:
    return DriftFinding(
        id=make_finding_id(scenario, "ec2_instance", resource),
        scenario_id=scenario,
        resource_type="ec2_instance",
        resource_id=resource,
        region="us-east-1",
        severity="medium",
        title="t",
        description="d",
        detected_at="2026-05-12T00:00:00+00:00",
        last_seen_at="2026-05-12T00:00:00+00:00",
        status=status,
    )


@pytest.mark.asyncio
async def test_drift_store_upserts_findings_by_id():
    s = DriftStore()
    counts = await s.apply_scan([_finding("open-ssh", "sg-1"), _finding("public-s3", "b-1")])
    assert counts == {"new": 2, "updated": 0, "resolved": 0, "total": 2}

    # Re-scan same resources — should update, not create
    counts = await s.apply_scan([_finding("open-ssh", "sg-1"), _finding("public-s3", "b-1")])
    assert counts["new"] == 0
    assert counts["updated"] == 2
    assert counts["total"] == 2


@pytest.mark.asyncio
async def test_drift_store_marks_disappeared_findings_resolved():
    s = DriftStore()
    await s.apply_scan([_finding("open-ssh", "sg-1"), _finding("public-s3", "b-1")])
    # Next scan only sees one of them
    counts = await s.apply_scan([_finding("open-ssh", "sg-1")])
    assert counts["resolved"] == 1
    resolved = [f for f in s.list_findings() if f.status == "resolved"]
    assert len(resolved) == 1
    assert resolved[0].scenario_id == "public-s3"


@pytest.mark.asyncio
async def test_drift_store_reopens_resolved_finding_if_seen_again():
    s = DriftStore()
    await s.apply_scan([_finding("open-ssh", "sg-1")])
    await s.apply_scan([])  # sg-1 now resolved
    assert s.list_findings()[0].status == "resolved"

    await s.apply_scan([_finding("open-ssh", "sg-1")])  # reappears
    findings = s.list_findings()
    assert findings[0].status == "open"
    assert findings[0].run_id is None


@pytest.mark.asyncio
async def test_drift_store_mark_remediating_sets_run_id():
    s = DriftStore()
    await s.apply_scan([_finding("open-ssh", "sg-1")])
    fid = make_finding_id("open-ssh", "ec2_instance", "sg-1")
    updated = await s.mark_remediating(fid, "run_abc")
    assert updated is not None
    assert updated.status == "remediating"
    assert updated.run_id == "run_abc"


@pytest.mark.asyncio
async def test_drift_store_preserves_remediating_status_on_rescan():
    """If a finding is being remediated, a re-scan that still sees it should
    not reset the status — the agent may still be working."""
    s = DriftStore()
    await s.apply_scan([_finding("open-ssh", "sg-1")])
    fid = make_finding_id("open-ssh", "ec2_instance", "sg-1")
    await s.mark_remediating(fid, "run_abc")

    await s.apply_scan([_finding("open-ssh", "sg-1")])
    assert s.get(fid).status == "remediating"
    assert s.get(fid).run_id == "run_abc"


def test_drift_store_sorts_open_before_resolved():
    s = DriftStore()
    s._findings["a"] = _finding("open-ssh", "a", status="resolved")
    s._findings["a"].detected_at = "2026-01-01T00:00:00+00:00"
    s._findings["b"] = _finding("open-ssh", "b", status="open")
    s._findings["b"].detected_at = "2025-01-01T00:00:00+00:00"
    s._findings["c"] = _finding("open-ssh", "c", status="remediating")
    s._findings["c"].detected_at = "2024-01-01T00:00:00+00:00"
    order = [f.resource_id for f in s.list_findings()]
    # open first, then remediating, then resolved — even though resolved is newer.
    assert order == ["b", "c", "a"]


# ---------------------------------------------------------------------------
# AwsDriftScanner with moto-stubbed boto3
# ---------------------------------------------------------------------------


pytest.importorskip("moto", reason="moto required for AWS scanner tests")
pytest.importorskip("boto3", reason="boto3 required for AWS scanner tests")

import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402


@pytest.fixture(autouse=True)
def _moto_env():
    """Give boto3 dummy creds so it doesn't try real auth in moto contexts."""
    prev = {k: os.environ.get(k) for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_DEFAULT_REGION")}
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_SESSION_TOKEN"] = "testing"
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
    yield
    for k, v in prev.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.mark.asyncio
async def test_aws_scanner_flags_open_ssh_security_group():
    with mock_aws():
        ec2 = boto3.client("ec2", region_name="us-east-1")
        vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
        sg = ec2.create_security_group(GroupName="web-sg", Description="x", VpcId=vpc)["GroupId"]
        ec2.authorize_security_group_ingress(
            GroupId=sg,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 22,
                    "ToPort": 22,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                }
            ],
        )
        scanner = AwsDriftScanner(region="us-east-1")
        findings = await scanner.scan()

    ssh_findings = [f for f in findings if f.scenario_id == "open-ssh"]
    assert len(ssh_findings) == 1
    assert ssh_findings[0].resource_id == sg
    assert "0.0.0.0/0" in ssh_findings[0].evidence["cidrs"]


@pytest.mark.asyncio
async def test_aws_scanner_does_not_flag_restricted_ssh():
    with mock_aws():
        ec2 = boto3.client("ec2", region_name="us-east-1")
        vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
        sg = ec2.create_security_group(GroupName="ok-sg", Description="x", VpcId=vpc)["GroupId"]
        ec2.authorize_security_group_ingress(
            GroupId=sg,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 22,
                    "ToPort": 22,
                    "IpRanges": [{"CidrIp": "10.0.0.0/8"}],
                }
            ],
        )
        scanner = AwsDriftScanner(region="us-east-1")
        findings = await scanner.scan()

    assert [f for f in findings if f.scenario_id == "open-ssh"] == []


@pytest.mark.asyncio
async def test_aws_scanner_flags_oversized_instance():
    with mock_aws():
        ec2 = boto3.client("ec2", region_name="us-east-1")
        ec2.run_instances(
            ImageId="ami-12345678",
            MinCount=1,
            MaxCount=1,
            InstanceType="m5.4xlarge",
            TagSpecifications=[
                {
                    "ResourceType": "instance",
                    "Tags": [
                        {"Key": "Environment", "Value": "staging"},
                        {"Key": "Owner", "Value": "platform"},
                        {"Key": "CostCenter", "Value": "eng"},
                    ],
                }
            ],
        )
        scanner = AwsDriftScanner(region="us-east-1")
        findings = await scanner.scan()

    idle = [f for f in findings if f.scenario_id == "idle-compute"]
    assert len(idle) == 1
    assert idle[0].evidence["instance_type"] == "m5.4xlarge"


@pytest.mark.asyncio
async def test_aws_scanner_flags_missing_tags_on_ec2():
    with mock_aws():
        ec2 = boto3.client("ec2", region_name="us-east-1")
        ec2.run_instances(
            ImageId="ami-12345678",
            MinCount=1,
            MaxCount=1,
            InstanceType="t3.medium",
            TagSpecifications=[
                {
                    "ResourceType": "instance",
                    "Tags": [{"Key": "Environment", "Value": "lab"}],
                }
            ],
        )
        scanner = AwsDriftScanner(region="us-east-1")
        findings = await scanner.scan()

    tag_findings = [f for f in findings if f.scenario_id == "missing-tags"]
    assert len(tag_findings) == 1
    missing = tag_findings[0].evidence["missing_tags"]
    assert "Owner" in missing
    assert "CostCenter" in missing
    assert "Environment" not in missing


@pytest.mark.asyncio
async def test_aws_scanner_flags_public_s3_bucket():
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="leaky-bucket")
        # No PublicAccessBlock is configured by default in moto → should flag.
        scanner = AwsDriftScanner(region="us-east-1")
        findings = await scanner.scan()

    s3_findings = [f for f in findings if f.scenario_id == "public-s3"]
    assert any(f.resource_id == "leaky-bucket" for f in s3_findings)


@pytest.mark.asyncio
async def test_aws_scanner_skips_bucket_with_public_access_block():
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="locked-down")
        s3.put_public_access_block(
            Bucket="locked-down",
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            },
        )
        s3.put_bucket_tagging(
            Bucket="locked-down",
            Tagging={
                "TagSet": [
                    {"Key": "Environment", "Value": "lab"},
                    {"Key": "Owner", "Value": "platform"},
                    {"Key": "CostCenter", "Value": "eng"},
                ]
            },
        )
        scanner = AwsDriftScanner(region="us-east-1")
        findings = await scanner.scan()

    assert [f for f in findings if f.scenario_id == "public-s3"] == []


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def test_factory_returns_mock_when_aws_drift_disabled(monkeypatch):
    from infraguard import config as cfg

    monkeypatch.setattr(cfg.settings, "aws_drift_enabled", False)
    scanner = build_scanner_from_settings()
    assert scanner.name == "mock"


def test_factory_returns_aws_when_drift_enabled(monkeypatch):
    from infraguard import config as cfg

    monkeypatch.setattr(cfg.settings, "aws_drift_enabled", True)
    monkeypatch.setattr(cfg.settings, "aws_region", "us-west-2")
    scanner = build_scanner_from_settings()
    assert scanner.name == "aws"


# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------


client = TestClient(app)


@pytest.fixture(autouse=True)
def _reset_drift_route_singleton():
    reset_drift_scanner()
    yield
    reset_drift_scanner()


def test_drift_scan_endpoint_returns_counts():
    resp = client.post("/drift/scan")
    assert resp.status_code == 200
    body = resp.json()
    assert body["scanner"] in ("mock", "aws")
    assert "counts" in body
    assert body["counts"]["total"] >= 0


def test_drift_list_endpoint_includes_scanner_metadata():
    client.post("/drift/scan")
    resp = client.get("/drift")
    assert resp.status_code == 200
    body = resp.json()
    assert body["scanner"] in ("mock", "aws")
    assert "findings" in body
    assert isinstance(body["findings"], list)


def test_drift_remediate_unknown_finding_returns_404():
    resp = client.post("/drift/does-not-exist/remediate")
    assert resp.status_code == 404


def test_health_includes_drift_fields():
    resp = client.get("/health")
    body = resp.json()
    assert "drift_scanner" in body
    assert "aws_drift_enabled" in body
    assert "aws_region" in body
