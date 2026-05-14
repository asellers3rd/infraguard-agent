"""Scenario registry. Builds zips from terraform-lab/<scenario>/ for mounting into the agent container."""
from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .config import TERRAFORM_LAB_DIR


@dataclass(frozen=True)
class Scenario:
    id: str
    label: str
    description: str
    severity: str  # low | medium | high | critical
    terraform_dir: str  # directory name under terraform-lab/
    expected_metrics: dict


SCENARIOS: list[Scenario] = [
    Scenario(
        id="open-ssh",
        label="Open SSH Ingress",
        description="Security group allows SSH (port 22) from 0.0.0.0/0",
        severity="critical",
        terraform_dir="open-ssh",
        expected_metrics={"timeToFirstToken": 340, "timeToPR": 12400, "estimatedCost": 0.08},
    ),
    Scenario(
        id="missing-tags",
        label="Missing Resource Tags",
        description="EC2 and RDS instances missing required Environment, Owner, CostCenter tags",
        severity="medium",
        terraform_dir="missing-tags",
        expected_metrics={"timeToFirstToken": 280, "timeToPR": 10800, "estimatedCost": 0.06},
    ),
    Scenario(
        id="public-s3",
        label="Public S3 Bucket",
        description="S3 bucket has public-read ACL and no public access block",
        severity="high",
        terraform_dir="public-s3",
        expected_metrics={"timeToFirstToken": 310, "timeToPR": 11200, "estimatedCost": 0.07},
    ),
    Scenario(
        id="idle-compute",
        label="Oversized Idle Compute",
        description="Always-on m5.4xlarge (~$560/mo) with no auto-scaling or scheduling",
        severity="low",
        terraform_dir="idle-compute",
        expected_metrics={"timeToFirstToken": 360, "timeToPR": 13600, "estimatedCost": 0.09},
    ),
]

_SCENARIOS_BY_ID = {s.id: s for s in SCENARIOS}


def get_scenario(scenario_id: str) -> Scenario | None:
    return _SCENARIOS_BY_ID.get(scenario_id)


def list_scenarios() -> list[Scenario]:
    return list(SCENARIOS)


def scenario_to_dict(scenario: Scenario) -> dict:
    return {
        "id": scenario.id,
        "label": scenario.label,
        "description": scenario.description,
        "severity": scenario.severity,
        "metrics": scenario.expected_metrics,
    }


def build_scenario_zip(scenario: Scenario) -> bytes:
    """Build an in-memory zip of the scenario's Terraform files.

    The zip is mounted into the agent container under /mnt/session/uploads/repo.zip
    and unzipped by the agent via bash before analysis.
    """
    source_dir = TERRAFORM_LAB_DIR / scenario.terraform_dir
    if not source_dir.exists():
        raise FileNotFoundError(f"Scenario source directory not found: {source_dir}")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(source_dir.rglob("*")):
            if path.is_file() and not _is_ignored(path):
                arcname = Path(scenario.terraform_dir) / path.relative_to(source_dir)
                zf.write(path, arcname=str(arcname))
    buf.seek(0)
    return buf.getvalue()


def _is_ignored(path: Path) -> bool:
    parts = set(path.parts)
    if parts & {".terraform", "__pycache__", ".git"}:
        return True
    if path.suffix in {".tfstate", ".tfplan"}:
        return True
    return path.name == ".terraform.lock.hcl"
