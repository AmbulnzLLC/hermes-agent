"""Tests for ``docker/seed_admin_config.py``.

The seed script runs at container boot in pilot/multi-tenant deployments to
inject admin-controlled config (bedrock guardrail + model selection).  Its
contract:

- Silent no-op when no admin env is set (generic deploys aren't pilot).
- Idempotent — only writes when the resulting dict differs from disk.
- Trace value is normalized to upper-case; invalid trace crashes the boot
  so misconfigurations don't silently disable guardrail observability.
- Removing ``BEDROCK_GUARDRAIL_TRACE`` clears any previously-seeded trace
  so operators can roll back without hand-editing config.yaml.
- HERMES_MODEL, when set, overwrites the top-level ``model`` key on every
  boot — admin (Helm) wins.  When the existing value differs, a WARNING
  is logged so users who edited config inside the pod see why it reverted.
- The dict-shape variant ``model: {default: ...}`` has its ``default`` leaf
  updated in place (preserving sibling keys), not collapsed to a string.
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


@pytest.fixture
def clean_env(monkeypatch):
    for var in (
        "BEDROCK_GUARDRAIL_ID",
        "BEDROCK_GUARDRAIL_VERSION",
        "BEDROCK_GUARDRAIL_TRACE",
        "HERMES_MODEL",
        "HERMES_ADMIN_CONFIG_OVERLAY",
    ):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


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



# ─── HERMES_MODEL seeding ───────────────────────────────────────────────────


def test_hermes_model_unset_is_noop(hermes_home, clean_env):
    """No HERMES_MODEL, no guardrail env → completely silent, no file."""
    mod = _load_module()
    assert mod.main() == 0
    assert not (hermes_home / "config.yaml").exists()


def test_hermes_model_seeds_string_when_no_existing_config(
    hermes_home, clean_env, monkeypatch
):
    monkeypatch.setenv("HERMES_MODEL", "us.anthropic.claude-opus-4-7")
    mod = _load_module()
    assert mod.main() == 0
    cfg = _read_cfg(hermes_home)
    assert cfg["model"] == "us.anthropic.claude-opus-4-7"


def test_hermes_model_seeds_alongside_guardrail(hermes_home, clean_env, monkeypatch):
    """Both subsystems can seed in a single boot."""
    monkeypatch.setenv("HERMES_MODEL", "us.anthropic.claude-sonnet-4")
    monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", "gr-abc")
    monkeypatch.setenv("BEDROCK_GUARDRAIL_VERSION", "DRAFT")

    mod = _load_module()
    assert mod.main() == 0
    cfg = _read_cfg(hermes_home)
    assert cfg["model"] == "us.anthropic.claude-sonnet-4"
    assert cfg["bedrock"]["guardrail"]["guardrail_identifier"] == "gr-abc"


def test_hermes_model_overwrites_existing_string_with_warning(
    hermes_home, clean_env, monkeypatch, capsys
):
    """User edited model in-pod → admin overwrites, but logs a warning."""
    cfg_path = hermes_home / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"model": "us.anthropic.claude-haiku-3"}))

    monkeypatch.setenv("HERMES_MODEL", "us.anthropic.claude-opus-4-7")
    mod = _load_module()
    assert mod.main() == 0

    cfg = _read_cfg(hermes_home)
    assert cfg["model"] == "us.anthropic.claude-opus-4-7"

    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "haiku" in err  # prior value mentioned
    assert "opus" in err   # new value mentioned


def test_hermes_model_idempotent_when_matching(hermes_home, clean_env, monkeypatch):
    """Same value already on disk → no rewrite, no mtime churn."""
    cfg_path = hermes_home / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"model": "us.anthropic.claude-opus-4-7"}))
    original_mtime = cfg_path.stat().st_mtime_ns

    monkeypatch.setenv("HERMES_MODEL", "us.anthropic.claude-opus-4-7")
    mod = _load_module()
    assert mod.main() == 0
    assert cfg_path.stat().st_mtime_ns == original_mtime


def test_hermes_model_no_warning_when_matching(
    hermes_home, clean_env, monkeypatch, capsys
):
    """Idempotent boot must not spuriously warn."""
    cfg_path = hermes_home / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"model": "us.anthropic.claude-opus-4-7"}))

    monkeypatch.setenv("HERMES_MODEL", "us.anthropic.claude-opus-4-7")
    mod = _load_module()
    assert mod.main() == 0

    err = capsys.readouterr().err
    assert "WARNING" not in err


def test_hermes_model_dict_shape_updates_default_leaf(
    hermes_home, clean_env, monkeypatch
):
    """Hermes' dict-shape config (model: {default: ..., base_url: ...}) →
    update only the default leaf, preserve siblings."""
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

    monkeypatch.setenv("HERMES_MODEL", "us.anthropic.claude-opus-4-7")
    mod = _load_module()
    assert mod.main() == 0

    cfg = _read_cfg(hermes_home)
    assert cfg["model"]["default"] == "us.anthropic.claude-opus-4-7"
    assert cfg["model"]["base_url"] == "https://bedrock-runtime.us-east-1.amazonaws.com"
    assert cfg["model"]["provider"] == "bedrock"


def test_hermes_model_dict_shape_warns_on_overwrite(
    hermes_home, clean_env, monkeypatch, capsys
):
    cfg_path = hermes_home / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump({"model": {"default": "us.anthropic.claude-haiku-3"}})
    )

    monkeypatch.setenv("HERMES_MODEL", "us.anthropic.claude-opus-4-7")
    mod = _load_module()
    assert mod.main() == 0

    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "model.default" in err


def test_hermes_model_preserves_other_keys(hermes_home, clean_env, monkeypatch):
    """Top-level keys other than model/bedrock survive untouched."""
    cfg_path = hermes_home / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "model": "old-model",
                "approvals": {"low_trust_mode": True},
                "display": {"personality": "vigo"},
                "bedrock": {"region": "us-east-1"},
            }
        )
    )

    monkeypatch.setenv("HERMES_MODEL", "new-model")
    mod = _load_module()
    assert mod.main() == 0

    cfg = _read_cfg(hermes_home)
    assert cfg["model"] == "new-model"
    assert cfg["approvals"] == {"low_trust_mode": True}
    assert cfg["display"]["personality"] == "vigo"
    assert cfg["bedrock"]["region"] == "us-east-1"


def test_hermes_model_empty_string_is_noop(hermes_home, clean_env, monkeypatch):
    """HERMES_MODEL='' is treated as unset — don't clobber config."""
    cfg_path = hermes_home / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"model": "us.anthropic.claude-opus-4-7"}))
    original_mtime = cfg_path.stat().st_mtime_ns

    monkeypatch.setenv("HERMES_MODEL", "   ")  # whitespace-only also stripped → empty
    mod = _load_module()
    assert mod.main() == 0

    cfg = _read_cfg(hermes_home)
    assert cfg["model"] == "us.anthropic.claude-opus-4-7"
    assert cfg_path.stat().st_mtime_ns == original_mtime


def test_hermes_model_alone_does_not_create_bedrock_block(
    hermes_home, clean_env, monkeypatch
):
    """Seeding model only must not synthesize an empty bedrock.guardrail block."""
    monkeypatch.setenv("HERMES_MODEL", "us.anthropic.claude-opus-4-7")
    mod = _load_module()
    assert mod.main() == 0

    cfg = _read_cfg(hermes_home)
    assert "bedrock" not in cfg


# ─── generic overlay deep-merge ─────────────────────────────────────────────


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


def test_overlay_absent_is_noop(hermes_home, clean_env, monkeypatch):
    """No overlay file, no other admin env → silent no-op, no file written."""
    monkeypatch.setenv(
        "HERMES_ADMIN_CONFIG_OVERLAY", str(hermes_home / "does-not-exist.yaml")
    )
    mod = _load_module()
    assert mod.main() == 0
    assert not (hermes_home / "config.yaml").exists()


def test_overlay_seeds_arbitrary_keys_from_scratch(
    hermes_home, clean_env, monkeypatch
):
    """Overlay alone (no guardrail/model env) can create config from nothing."""
    _write_overlay(
        hermes_home,
        monkeypatch,
        {
            "platforms": {"teams": {"typing_indicator": False}},
            "some_new_top_level": {"nested": {"value": 42}},
        },
    )
    mod = _load_module()
    assert mod.main() == 0

    cfg = _read_cfg(hermes_home)
    assert cfg["platforms"]["teams"]["typing_indicator"] is False
    assert cfg["some_new_top_level"]["nested"]["value"] == 42


def test_overlay_deep_merges_preserving_siblings(
    hermes_home, clean_env, monkeypatch
):
    """Nested overlay merges key-by-key; adjacent keys survive."""
    cfg_path = hermes_home / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "platforms": {
                    "teams": {"enabled": True, "existing_key": "keep"},
                },
                "approvals": {"mode": "off"},
            }
        )
    )
    _write_overlay(
        hermes_home,
        monkeypatch,
        {"platforms": {"teams": {"typing_indicator": False}}},
    )
    mod = _load_module()
    assert mod.main() == 0

    cfg = _read_cfg(hermes_home)
    # Overlay added the new nested key...
    assert cfg["platforms"]["teams"]["typing_indicator"] is False
    # ...without disturbing siblings at any level.
    assert cfg["platforms"]["teams"]["enabled"] is True
    assert cfg["platforms"]["teams"]["existing_key"] == "keep"
    assert cfg["approvals"]["mode"] == "off"


def test_overlay_scalar_replaces_base(hermes_home, clean_env, monkeypatch):
    """A scalar in the overlay replaces the base value at that path."""
    cfg_path = hermes_home / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"approvals": {"mode": "off"}}))
    _write_overlay(hermes_home, monkeypatch, {"approvals": {"mode": "on"}})
    mod = _load_module()
    assert mod.main() == 0
    assert _read_cfg(hermes_home)["approvals"]["mode"] == "on"


def test_overlay_list_replaces_not_concatenates(
    hermes_home, clean_env, monkeypatch
):
    """Lists are replaced wholesale, never concatenated."""
    cfg_path = hermes_home / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"toolsets": ["a", "b", "c"]}))
    _write_overlay(hermes_home, monkeypatch, {"toolsets": ["x"]})
    mod = _load_module()
    assert mod.main() == 0
    assert _read_cfg(hermes_home)["toolsets"] == ["x"]


def test_overlay_null_overwrites(hermes_home, clean_env, monkeypatch):
    """An explicit null in the overlay blanks the base value."""
    cfg_path = hermes_home / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"feature": {"flag": "enabled"}}))
    # YAML `flag: null` → Python None
    _write_overlay(hermes_home, monkeypatch, "feature:\n  flag: null\n")
    mod = _load_module()
    assert mod.main() == 0
    cfg = _read_cfg(hermes_home)
    assert "flag" in cfg["feature"]
    assert cfg["feature"]["flag"] is None


def test_overlay_wins_over_model_seeder(hermes_home, clean_env, monkeypatch):
    """Overlay is applied last, so it beats HERMES_MODEL on the same key."""
    monkeypatch.setenv("HERMES_MODEL", "env-model")
    _write_overlay(hermes_home, monkeypatch, {"model": "overlay-model"})
    mod = _load_module()
    assert mod.main() == 0
    assert _read_cfg(hermes_home)["model"] == "overlay-model"


def test_overlay_applies_alongside_guardrail(hermes_home, clean_env, monkeypatch):
    """Explicit seeders and overlay coexist when they touch disjoint keys."""
    monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", "gr-abc")
    monkeypatch.setenv("BEDROCK_GUARDRAIL_VERSION", "DRAFT")
    _write_overlay(
        hermes_home, monkeypatch, {"display": {"personality": "vigo"}}
    )
    mod = _load_module()
    assert mod.main() == 0
    cfg = _read_cfg(hermes_home)
    assert cfg["bedrock"]["guardrail"]["guardrail_identifier"] == "gr-abc"
    assert cfg["display"]["personality"] == "vigo"


def test_overlay_empty_file_is_noop(hermes_home, clean_env, monkeypatch):
    """An empty (or whitespace-only) overlay file merges nothing."""
    cfg_path = hermes_home / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"model": "keep-me"}))
    original_mtime = cfg_path.stat().st_mtime_ns
    _write_overlay(hermes_home, monkeypatch, "   \n")
    mod = _load_module()
    assert mod.main() == 0
    assert _read_cfg(hermes_home)["model"] == "keep-me"
    assert cfg_path.stat().st_mtime_ns == original_mtime


def test_overlay_non_mapping_is_skipped(hermes_home, clean_env, monkeypatch, capsys):
    """A top-level list/scalar overlay is rejected (logged), config untouched."""
    cfg_path = hermes_home / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"model": "keep-me"}))
    _write_overlay(hermes_home, monkeypatch, "- just\n- a\n- list\n")
    mod = _load_module()
    assert mod.main() == 0
    assert _read_cfg(hermes_home)["model"] == "keep-me"
    assert "WARNING" in capsys.readouterr().err


def test_overlay_broken_yaml_fails_open(hermes_home, clean_env, monkeypatch, capsys):
    """Unparseable overlay is skipped; existing config survives intact."""
    cfg_path = hermes_home / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"model": "keep-me"}))
    _write_overlay(hermes_home, monkeypatch, "this: is: not: valid: [unbalanced")
    mod = _load_module()
    assert mod.main() == 0
    assert _read_cfg(hermes_home)["model"] == "keep-me"
    assert "WARNING" in capsys.readouterr().err


def test_overlay_idempotent_when_matching(hermes_home, clean_env, monkeypatch):
    """Overlay already reflected on disk → no rewrite, no mtime churn."""
    cfg_path = hermes_home / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"display": {"personality": "vigo"}}))
    original_mtime = cfg_path.stat().st_mtime_ns
    _write_overlay(
        hermes_home, monkeypatch, {"display": {"personality": "vigo"}}
    )
    mod = _load_module()
    assert mod.main() == 0
    assert cfg_path.stat().st_mtime_ns == original_mtime


def test_deep_merge_helper_unit():
    """Direct unit coverage of the merge primitive."""
    mod = _load_module()
    base = {"a": {"b": 1, "c": 2}, "d": [1, 2], "e": "old"}
    mod._deep_merge(base, {"a": {"c": 99, "x": 7}, "d": [3], "e": "new"})
    assert base == {"a": {"b": 1, "c": 99, "x": 7}, "d": [3], "e": "new"}
