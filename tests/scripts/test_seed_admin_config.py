"""Tests for ``docker/seed_admin_config.py``.

The seed script runs at container boot in pilot/multi-tenant deployments to
apply an admin-controlled config overlay (deep-merged into config.yaml).  Its
contract:

- Silent no-op when the overlay file is absent (generic deploys aren't pilot).
- Overlay is recursively deep-merged: nested maps merge key-by-key; scalars,
  lists, and ``None`` replace the value at that path.
- Idempotent — only writes when the merged result differs from disk.
- Compliance guarantee: ``bedrock.guardrail.trace`` in the merged config is
  validated (source-agnostic) and normalized to upper-case; an invalid value
  crashes the boot so guardrail observability can't be silently disabled.
- Fail-open: a missing/empty/non-mapping/unparseable overlay, or a pre-broken
  config.yaml, is skipped/left untouched rather than clobbered.
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
    # Default overlay to a path under tmp that does not exist, so tests are
    # hermetic against any real /config/overlay.yaml on the host/CI runner.
    # Overlay-specific tests override this env to point at a file they create.
    monkeypatch.setenv(
        "HERMES_ADMIN_CONFIG_OVERLAY", str(tmp_path / "no-such-overlay.yaml")
    )
    return tmp_path


def _read_cfg(home: Path) -> dict:
    p = home / "config.yaml"
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text()) or {}


def _write_overlay(home: Path, monkeypatch, data) -> Path:
    """Write an overlay file under ``home`` and point the env at it.

    ``data`` may be a dict (dumped to YAML) or a raw string (written verbatim,
    for malformed-YAML cases).
    """
    p = home / "overlay.yaml"
    if isinstance(data, str):
        p.write_text(data)
    else:
        p.write_text(yaml.safe_dump(data))
    monkeypatch.setenv("HERMES_ADMIN_CONFIG_OVERLAY", str(p))
    return p


# ─── no-op / absent overlay ─────────────────────────────────────────────────


def test_absent_overlay_is_noop(hermes_home, monkeypatch):
    """No overlay file → silent no-op, no config.yaml written."""
    monkeypatch.setenv(
        "HERMES_ADMIN_CONFIG_OVERLAY", str(hermes_home / "does-not-exist.yaml")
    )
    mod = _load_module()
    assert mod.main() == 0
    assert not (hermes_home / "config.yaml").exists()


def test_empty_overlay_file_is_noop(hermes_home, monkeypatch):
    """An empty (whitespace-only) overlay merges nothing, no mtime churn."""
    cfg_path = hermes_home / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"model": "keep-me"}))
    original_mtime = cfg_path.stat().st_mtime_ns
    _write_overlay(hermes_home, monkeypatch, "   \n")
    mod = _load_module()
    assert mod.main() == 0
    assert _read_cfg(hermes_home)["model"] == "keep-me"
    assert cfg_path.stat().st_mtime_ns == original_mtime


# ─── deep-merge semantics ───────────────────────────────────────────────────


def test_overlay_seeds_arbitrary_keys_from_scratch(hermes_home, monkeypatch):
    """Overlay can create config from nothing."""
    _write_overlay(
        hermes_home,
        monkeypatch,
        {
            "model": "us.anthropic.claude-sonnet-4-5",
            "bedrock": {"guardrail": {"guardrail_identifier": "gr-abc", "guardrail_version": "DRAFT"}},
            "some_new_top_level": {"nested": {"value": 42}},
        },
    )
    mod = _load_module()
    assert mod.main() == 0

    cfg = _read_cfg(hermes_home)
    assert cfg["model"] == "us.anthropic.claude-sonnet-4-5"
    assert cfg["bedrock"]["guardrail"]["guardrail_identifier"] == "gr-abc"
    assert cfg["some_new_top_level"]["nested"]["value"] == 42


def test_overlay_deep_merges_preserving_siblings(hermes_home, monkeypatch):
    """Nested overlay merges key-by-key; adjacent keys survive."""
    cfg_path = hermes_home / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "platforms": {"teams": {"enabled": True, "existing_key": "keep"}},
                "approvals": {"mode": "off"},
            }
        )
    )
    _write_overlay(
        hermes_home, monkeypatch, {"platforms": {"teams": {"typing_indicator": False}}}
    )
    mod = _load_module()
    assert mod.main() == 0

    cfg = _read_cfg(hermes_home)
    assert cfg["platforms"]["teams"]["typing_indicator"] is False
    assert cfg["platforms"]["teams"]["enabled"] is True
    assert cfg["platforms"]["teams"]["existing_key"] == "keep"
    assert cfg["approvals"]["mode"] == "off"


def test_overlay_scalar_replaces_base(hermes_home, monkeypatch):
    cfg_path = hermes_home / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"approvals": {"mode": "off"}}))
    _write_overlay(hermes_home, monkeypatch, {"approvals": {"mode": "on"}})
    mod = _load_module()
    assert mod.main() == 0
    assert _read_cfg(hermes_home)["approvals"]["mode"] == "on"


def test_overlay_list_replaces_not_concatenates(hermes_home, monkeypatch):
    cfg_path = hermes_home / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"toolsets": ["a", "b", "c"]}))
    _write_overlay(hermes_home, monkeypatch, {"toolsets": ["x"]})
    mod = _load_module()
    assert mod.main() == 0
    assert _read_cfg(hermes_home)["toolsets"] == ["x"]


def test_overlay_null_overwrites(hermes_home, monkeypatch):
    """An explicit null in the overlay blanks a NON-guardrail-trace key."""
    cfg_path = hermes_home / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"feature": {"flag": "enabled"}}))
    _write_overlay(hermes_home, monkeypatch, "feature:\n  flag: null\n")
    mod = _load_module()
    assert mod.main() == 0
    cfg = _read_cfg(hermes_home)
    assert "flag" in cfg["feature"]
    assert cfg["feature"]["flag"] is None


def test_overlay_model_string_shape(hermes_home, monkeypatch):
    """Overlay writes a top-level string model (Helm configmap shape)."""
    _write_overlay(hermes_home, monkeypatch, {"model": "us.anthropic.claude-opus-4-8"})
    mod = _load_module()
    assert mod.main() == 0
    assert _read_cfg(hermes_home)["model"] == "us.anthropic.claude-opus-4-8"


def test_overlay_model_dict_shape_deep_merges(hermes_home, monkeypatch):
    """A dict-shape model on disk deep-merges: default leaf updated, siblings kept."""
    cfg_path = hermes_home / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "model": {
                    "default": "us.anthropic.claude-haiku-3",
                    "base_url": "https://bedrock-runtime.us-east-1.amazonaws.com",
                    "provider": "bedrock",
                }
            }
        )
    )
    # Overlay templated as the same dict shape (chart controls the shape).
    _write_overlay(
        hermes_home, monkeypatch, {"model": {"default": "us.anthropic.claude-opus-4-8"}}
    )
    mod = _load_module()
    assert mod.main() == 0
    cfg = _read_cfg(hermes_home)
    assert cfg["model"]["default"] == "us.anthropic.claude-opus-4-8"
    assert cfg["model"]["base_url"] == "https://bedrock-runtime.us-east-1.amazonaws.com"
    assert cfg["model"]["provider"] == "bedrock"


# ─── idempotency ────────────────────────────────────────────────────────────


def test_idempotent_when_matching(hermes_home, monkeypatch):
    """Overlay already reflected on disk → no rewrite, no mtime churn."""
    cfg_path = hermes_home / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"display": {"personality": "vigo"}}))
    original_mtime = cfg_path.stat().st_mtime_ns
    _write_overlay(hermes_home, monkeypatch, {"display": {"personality": "vigo"}})
    mod = _load_module()
    assert mod.main() == 0
    assert cfg_path.stat().st_mtime_ns == original_mtime


def test_guardrail_idempotent_normalized_trace(hermes_home, monkeypatch):
    """Config already has upper-case trace matching the overlay → no rewrite."""
    cfg_path = hermes_home / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {"bedrock": {"guardrail": {"guardrail_identifier": "gr-abc", "guardrail_version": "DRAFT", "trace": "ENABLED"}}}
        )
    )
    original_mtime = cfg_path.stat().st_mtime_ns
    _write_overlay(
        hermes_home,
        monkeypatch,
        {"bedrock": {"guardrail": {"guardrail_identifier": "gr-abc", "guardrail_version": "DRAFT", "trace": "ENABLED"}}},
    )
    mod = _load_module()
    assert mod.main() == 0
    assert cfg_path.stat().st_mtime_ns == original_mtime


# ─── guardrail trace validation (compliance-critical, source-agnostic) ──────


def test_guardrail_trace_normalized_to_uppercase(hermes_home, monkeypatch):
    _write_overlay(
        hermes_home,
        monkeypatch,
        {"bedrock": {"guardrail": {"guardrail_identifier": "gr-abc", "guardrail_version": "DRAFT", "trace": "enabled"}}},
    )
    mod = _load_module()
    assert mod.main() == 0
    assert _read_cfg(hermes_home)["bedrock"]["guardrail"]["trace"] == "ENABLED"


@pytest.mark.parametrize("value", ["ENABLED", "ENABLED_FULL", "DISABLED", "enabled_full", "disabled"])
def test_guardrail_trace_accepts_all_valid_values(hermes_home, monkeypatch, value):
    _write_overlay(
        hermes_home,
        monkeypatch,
        {"bedrock": {"guardrail": {"guardrail_identifier": "gr-abc", "guardrail_version": "DRAFT", "trace": value}}},
    )
    mod = _load_module()
    assert mod.main() == 0
    assert _read_cfg(hermes_home)["bedrock"]["guardrail"]["trace"] == value.upper()


@pytest.mark.parametrize("bad", ["on", "off", "true", "yes", "trace"])
def test_invalid_guardrail_trace_crashes_boot(hermes_home, monkeypatch, bad):
    """Invalid trace must fail loud — guardrail observability is compliance-critical."""
    _write_overlay(
        hermes_home,
        monkeypatch,
        {"bedrock": {"guardrail": {"guardrail_identifier": "gr-abc", "guardrail_version": "DRAFT", "trace": bad}}},
    )
    mod = _load_module()
    assert mod.main() == 1


def test_invalid_trace_from_disk_also_crashes(hermes_home, monkeypatch):
    """Validation is source-agnostic: a bad trace already on disk crashes too,
    even if the overlay only touches an unrelated key."""
    cfg_path = hermes_home / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump({"bedrock": {"guardrail": {"guardrail_identifier": "gr-abc", "trace": "bogus"}}})
    )
    _write_overlay(hermes_home, monkeypatch, {"display": {"personality": "vigo"}})
    mod = _load_module()
    assert mod.main() == 1


def test_guardrail_trace_whitespace_stripped(hermes_home, monkeypatch):
    """Leading/trailing whitespace is stripped before validation."""
    _write_overlay(
        hermes_home,
        monkeypatch,
        {"bedrock": {"guardrail": {"trace": "  ENABLED  "}}},
    )
    mod = _load_module()
    assert mod.main() == 0
    assert _read_cfg(hermes_home)["bedrock"]["guardrail"]["trace"] == "ENABLED"


def test_guardrail_trace_null_drops_key(hermes_home, monkeypatch):
    """An explicit null trace is treated as no-trace and the key is removed."""
    cfg_path = hermes_home / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump({"bedrock": {"guardrail": {"guardrail_identifier": "gr-abc", "trace": "ENABLED"}}})
    )
    _write_overlay(hermes_home, monkeypatch, "bedrock:\n  guardrail:\n    trace: null\n")
    mod = _load_module()
    assert mod.main() == 0
    g = _read_cfg(hermes_home)["bedrock"]["guardrail"]
    assert "trace" not in g
    assert g["guardrail_identifier"] == "gr-abc"


def test_guardrail_without_trace_is_fine(hermes_home, monkeypatch):
    """No trace key → no validation, guardrail id/version seeded normally."""
    _write_overlay(
        hermes_home,
        monkeypatch,
        {"bedrock": {"guardrail": {"guardrail_identifier": "gr-abc", "guardrail_version": "DRAFT"}}},
    )
    mod = _load_module()
    assert mod.main() == 0
    g = _read_cfg(hermes_home)["bedrock"]["guardrail"]
    assert g["guardrail_identifier"] == "gr-abc"
    assert "trace" not in g


# ─── fail-open behavior ─────────────────────────────────────────────────────


def test_overlay_non_mapping_is_skipped(hermes_home, monkeypatch, capsys):
    cfg_path = hermes_home / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"model": "keep-me"}))
    _write_overlay(hermes_home, monkeypatch, "- just\n- a\n- list\n")
    mod = _load_module()
    assert mod.main() == 0
    assert _read_cfg(hermes_home)["model"] == "keep-me"
    assert "WARNING" in capsys.readouterr().err


def test_overlay_broken_yaml_fails_open(hermes_home, monkeypatch, capsys):
    cfg_path = hermes_home / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"model": "keep-me"}))
    _write_overlay(hermes_home, monkeypatch, "this: is: not: valid: [unbalanced")
    mod = _load_module()
    assert mod.main() == 0
    assert _read_cfg(hermes_home)["model"] == "keep-me"
    assert "WARNING" in capsys.readouterr().err


def test_broken_config_yaml_fails_open(hermes_home, monkeypatch, capsys):
    """A pre-broken config.yaml must not be clobbered — exit 0, log warning."""
    cfg_path = hermes_home / "config.yaml"
    cfg_path.write_text("this: is: not: valid: yaml: [unbalanced")
    original = cfg_path.read_bytes()
    _write_overlay(hermes_home, monkeypatch, {"model": "new"})
    mod = _load_module()
    assert mod.main() == 0
    assert cfg_path.read_bytes() == original
    assert "WARNING" in capsys.readouterr().err


# ─── helper unit ────────────────────────────────────────────────────────────


def test_deep_merge_helper_unit():
    mod = _load_module()
    base = {"a": {"b": 1, "c": 2}, "d": [1, 2], "e": "old"}
    mod._deep_merge(base, {"a": {"c": 99, "x": 7}, "d": [3], "e": "new"})
    assert base == {"a": {"b": 1, "c": 99, "x": 7}, "d": [3], "e": "new"}
