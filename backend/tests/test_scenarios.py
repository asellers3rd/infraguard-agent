"""Tests for scenario registry and zip building."""
from __future__ import annotations

import io
import zipfile

import pytest

from infraguard.scenarios import (
    SCENARIOS,
    build_scenario_zip,
    get_scenario,
    scenario_to_dict,
)


def test_four_scenarios_registered():
    assert len(SCENARIOS) == 4
    ids = {s.id for s in SCENARIOS}
    assert ids == {"open-ssh", "missing-tags", "public-s3", "idle-compute"}


def test_get_scenario_by_id():
    s = get_scenario("open-ssh")
    assert s is not None
    assert s.label == "Open SSH Ingress"
    assert s.severity == "critical"


def test_get_unknown_scenario_returns_none():
    assert get_scenario("does-not-exist") is None


def test_scenario_to_dict_shape():
    s = get_scenario("missing-tags")
    d = scenario_to_dict(s)
    assert set(d.keys()) == {"id", "label", "description", "severity", "metrics"}
    assert set(d["metrics"].keys()) == {"timeToFirstToken", "timeToPR", "estimatedCost"}


@pytest.mark.parametrize("scenario_id", ["open-ssh", "missing-tags", "public-s3", "idle-compute"])
def test_build_scenario_zip_contains_main_tf(scenario_id):
    scenario = get_scenario(scenario_id)
    zip_bytes = build_scenario_zip(scenario)

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        # Should contain at least main.tf inside the scenario directory
        assert any(name.endswith("main.tf") for name in names)
        # Should be prefixed with the scenario directory
        assert all(name.startswith(scenario.terraform_dir + "/") for name in names)


def test_build_scenario_zip_skips_terraform_state():
    """Even if .terraform/ existed, the zip should not include it."""
    scenario = get_scenario("open-ssh")
    zip_bytes = build_scenario_zip(scenario)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for name in zf.namelist():
            assert ".terraform" not in name
            assert not name.endswith(".tfstate")
            assert not name.endswith(".tfplan")
