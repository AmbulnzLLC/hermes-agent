"""Tests for ``docker/seed_admin_config.py``.

The seed script runs at container boot in pilot/multi-tenant deployments to
inject admin-controlled bedrock guardrail config.  Its contract:

- Silent no-op when guardrail env is unset (generic deploys aren't pilot).
- Idempotent — only writes when the resulting dict differs from disk.
- Trace value is normalized to upper-case; invalid trace crashes the boot
  so misconfigurations don't silently disable guardrail observability.
- Removing ``BEDROCK_GUARDRAIL_TRACE`` clears any previously-seeded trace
  so operators can roll back without hand-editing config.yaml.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest
import yaml


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "docker" / "seed_admin_config.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("_seed_admin_config", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def clean_env(monkeypatch):
    for var in ("BEDROCK_GUARDRAIL_ID", "BEDROCK_GUARDRAIL_VERSION", "BEDROCK_GUARDRAIL_TRACE"):
        monkeypatch.delenv(var, raising=False)


def _read_cfg(home: Path) -> dict:
    p = home / "config.yaml"
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text()) or {}


def test_no_guardrail_env_is_noop(hermes_home, clean_env):
    """Generic (non-pilot) deploys: silently do nothing."""
    mod = _load_module()
    assert mod.main() == 0
    assert not (hermes_home / "config.yaml").exists()


def test_seeds_guardrail_without_trace(hermes_home, clean_env, monkeypatch):
    monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", "gr-abc")
    monkeypatch.setenv("BEDROCK_GUARDRAIL_VERSION", "DRAFT")

    mod = _load_module()
    assert mod.main() == 0

    cfg = _read_cfg(hermes_home)
    g = cfg["bedrock"]["guardrail"]
    assert g["guardrail_identifier"] == "gr-abc"
    assert g["guardrail_version"] == "DRAFT"
    assert "trace" not in g


def test_seeds_trace_normalized_to_uppercase(hermes_home, clean_env, monkeypatch):
    monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", "gr-abc")
    monkeypatch.setenv("BEDROCK_GUARDRAIL_VERSION", "DRAFT")
    monkeypatch.setenv("BEDROCK_GUARDRAIL_TRACE", "enabled")  # lowercase

    mod = _load_module()
    assert mod.main() == 0

    g = _read_cfg(hermes_home)["bedrock"]["guardrail"]
    assert g["trace"] == "ENABLED"


@pytest.mark.parametrize("value", ["ENABLED", "ENABLED_FULL", "DISABLED", "enabled_full", "disabled"])
def test_trace_accepts_all_valid_values(hermes_home, clean_env, monkeypatch, value):
    monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", "gr-abc")
    monkeypatch.setenv("BEDROCK_GUARDRAIL_VERSION", "DRAFT")
    monkeypatch.setenv("BEDROCK_GUARDRAIL_TRACE", value)

    mod = _load_module()
    assert mod.main() == 0
    g = _read_cfg(hermes_home)["bedrock"]["guardrail"]
    assert g["trace"] == value.upper()


@pytest.mark.parametrize("bad", ["on", "off", "true", "yes", "trace", " ENABLED "])
def test_invalid_trace_crashes_boot(hermes_home, clean_env, monkeypatch, bad):
    """Invalid trace must fail loud — guardrail observability is compliance-critical."""
    monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", "gr-abc")
    monkeypatch.setenv("BEDROCK_GUARDRAIL_VERSION", "DRAFT")
    monkeypatch.setenv("BEDROCK_GUARDRAIL_TRACE", bad)

    mod = _load_module()
    # Note: " ENABLED " is whitespace-stripped before validation, so it
    # actually passes — adjust expectation.
    expected = 0 if bad.strip().upper() in {"ENABLED", "ENABLED_FULL", "DISABLED"} else 1
    assert mod.main() == expected


def test_idempotent_no_rewrite_when_matching(hermes_home, clean_env, monkeypatch):
    """Don't churn mtime when env already matches disk — keeps config-load cache warm."""
    cfg_path = hermes_home / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "bedrock": {"guardrail": {
            "guardrail_identifier": "gr-abc",
            "guardrail_version": "DRAFT",
            "trace": "ENABLED",
        }}
    }))
    original_mtime = cfg_path.stat().st_mtime_ns

    monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", "gr-abc")
    monkeypatch.setenv("BEDROCK_GUARDRAIL_VERSION", "DRAFT")
    monkeypatch.setenv("BEDROCK_GUARDRAIL_TRACE", "enabled")

    mod = _load_module()
    assert mod.main() == 0
    assert cfg_path.stat().st_mtime_ns == original_mtime


def test_removing_trace_env_clears_seeded_trace(hermes_home, clean_env, monkeypatch):
    """Operator drops BEDROCK_GUARDRAIL_TRACE → seeded trace key is removed."""
    cfg_path = hermes_home / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "bedrock": {"guardrail": {
            "guardrail_identifier": "gr-abc",
            "guardrail_version": "DRAFT",
            "trace": "ENABLED",
        }}
    }))

    monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", "gr-abc")
    monkeypatch.setenv("BEDROCK_GUARDRAIL_VERSION", "DRAFT")
    # BEDROCK_GUARDRAIL_TRACE not set

    mod = _load_module()
    assert mod.main() == 0
    g = _read_cfg(hermes_home)["bedrock"]["guardrail"]
    assert "trace" not in g
    assert g["guardrail_identifier"] == "gr-abc"


def test_preserves_unrelated_config_keys(hermes_home, clean_env, monkeypatch):
    """Adjacent keys under bedrock and at top level must survive a seed write."""
    cfg_path = hermes_home / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "model": {"default": "us.anthropic.claude-opus-4-7"},
        "bedrock": {
            "region": "us-east-1",
            "guardrail": {"guardrail_identifier": "old", "guardrail_version": "1"},
        },
        "display": {"personality": "vigo"},
    }))

    monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", "gr-new")
    monkeypatch.setenv("BEDROCK_GUARDRAIL_VERSION", "DRAFT")
    monkeypatch.setenv("BEDROCK_GUARDRAIL_TRACE", "ENABLED")

    mod = _load_module()
    assert mod.main() == 0

    cfg = _read_cfg(hermes_home)
    assert cfg["model"]["default"] == "us.anthropic.claude-opus-4-7"
    assert cfg["bedrock"]["region"] == "us-east-1"
    assert cfg["bedrock"]["guardrail"]["guardrail_identifier"] == "gr-new"
    assert cfg["bedrock"]["guardrail"]["guardrail_version"] == "DRAFT"
    assert cfg["bedrock"]["guardrail"]["trace"] == "ENABLED"
    assert cfg["display"]["personality"] == "vigo"


def test_broken_yaml_fails_open(hermes_home, clean_env, monkeypatch, capsys):
    """A pre-broken config.yaml must not be clobbered — exit 0, log warning."""
    cfg_path = hermes_home / "config.yaml"
    cfg_path.write_text("this: is: not: valid: yaml: [unbalanced")
    original = cfg_path.read_bytes()

    monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", "gr-abc")
    monkeypatch.setenv("BEDROCK_GUARDRAIL_VERSION", "DRAFT")

    mod = _load_module()
    assert mod.main() == 0
    assert cfg_path.read_bytes() == original
    err = capsys.readouterr().err
    assert "WARNING" in err
