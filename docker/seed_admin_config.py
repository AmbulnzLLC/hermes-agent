"""Seed the admin-managed config overlay at container boot.

Designed for pilot/multi-tenant container deployments where the operator
provisions config (identity, model selection, guardrails, and anything else)
via Kubernetes injection rather than baking it into the image.

The mechanism is a single generic overlay:

- ``HERMES_ADMIN_CONFIG_OVERLAY`` names a YAML file (default
  ``/config/overlay.yaml``), typically projected from a Helm-managed ConfigMap.
- On every boot its top-level mapping is **recursively deep-merged** into
  ``$HERMES_HOME/config.yaml``: nested mappings merge key-by-key; scalars,
  lists, and ``None`` REPLACE whatever is at that path (lists are not
  concatenated).  The overlay is unrestricted — it can pin *any* config key,
  so adding a new admin-controlled setting needs no code change here.
- Absent/empty overlay file → silent no-op (generic, non-pilot deploys).
- A non-mapping or unparseable overlay file is logged and skipped (fail-open) —
  it never clobbers a working config.yaml.

History: this script used to carry dedicated ``_seed_model`` /
``_seed_guardrail`` helpers driven by ``HERMES_MODEL`` / ``BEDROCK_GUARDRAIL_*``
env vars.  Those were folded into the generic overlay — the hermes-eks chart
now templates ``model`` and ``bedrock.guardrail.*`` into the overlay ConfigMap
directly, so there is a single source of truth for admin config.

Compliance guarantee preserved across the migration:
- ``bedrock.guardrail.trace``, when present in the merged config, must be one of
  ``ENABLED``, ``ENABLED_FULL``, ``DISABLED`` (case-insensitive; normalized to
  upper-case on write).  Anything else is rejected with a non-zero exit so a
  misconfigured guardrail crash-loops rather than silently disabling tracing.
  This validation is now source-agnostic: it fires whether the trace value came
  from the overlay or was already on disk.

General:
- Runs on every container boot.  Idempotent when the merged result already
  matches disk (no mtime churn → keeps ``load_config()``'s cache warm).
- Preserves all keys in config.yaml that the overlay does not touch.
- Fails open: a pre-existing YAML parse error in config.yaml causes this
  script to log and exit 0 rather than clobbering the broken file.

This is a SEED, not a lock.  A user can subsequently edit, blank, or remove
these values via ``hermes config set`` or by editing config.yaml directly; the
overlay re-applies on the next container boot (admin/Helm wins).  For guardrail
specifically, the actual enforcement that every Bedrock call carries the
guardrail must come from the IAM layer (``bedrock:GuardrailIdentifier``
condition keys on the role the pod assumes), not from this file.
"""
from __future__ import annotations

import copy
import os
import sys
from pathlib import Path

import yaml


_DEFAULT_OVERLAY_PATH = "/config/overlay.yaml"

_VALID_GUARDRAIL_TRACE = {"ENABLED", "ENABLED_FULL", "DISABLED"}


def _deep_merge(base: dict, overlay: dict) -> None:
    """Recursively merge ``overlay`` into ``base`` in place.

    Semantics:
    - When both sides hold a mapping at the same key, recurse.
    - Otherwise the overlay value REPLACES the base value.  This covers
      scalars, lists (no concatenation — the overlay list wins whole), and
      ``None`` (an explicit null in the overlay overwrites, letting an operator
      blank a key).
    """
    for key, ov in overlay.items():
        cur = base.get(key)
        if isinstance(cur, dict) and isinstance(ov, dict):
            _deep_merge(cur, ov)
        else:
            base[key] = ov


def _apply_overlay(cfg: dict) -> int:
    """Deep-merge the operator overlay file into ``cfg`` in place.

    Reads the YAML file named by ``HERMES_ADMIN_CONFIG_OVERLAY`` (default
    ``/config/overlay.yaml``).  Absent/empty file → no-op.  A non-mapping or
    unparseable overlay is logged and skipped (fail-open).

    Returns 0 always — a malformed overlay must not crash the boot; it just
    doesn't apply.  (Compliance-critical validation of specific merged values,
    e.g. the guardrail trace, happens separately in ``_validate_guardrail_trace``
    so it fires regardless of where the value came from.)
    """
    overlay_path = Path(
        os.environ.get("HERMES_ADMIN_CONFIG_OVERLAY", _DEFAULT_OVERLAY_PATH)
    )
    if not overlay_path.exists():
        return 0

    try:
        loaded = yaml.safe_load(overlay_path.read_text())
    except yaml.YAMLError as exc:
        print(
            f"[seed_admin_config] WARNING: overlay {overlay_path} parse failed "
            f"({exc}); skipping overlay",
            file=sys.stderr,
        )
        return 0

    if loaded is None:
        # Empty file — nothing to merge.
        return 0
    if not isinstance(loaded, dict):
        print(
            f"[seed_admin_config] WARNING: overlay {overlay_path} did not parse "
            f"as a mapping (got {type(loaded).__name__}); skipping overlay",
            file=sys.stderr,
        )
        return 0

    _deep_merge(cfg, loaded)
    return 0


def _validate_guardrail_trace(cfg: dict) -> int:
    """Validate + normalize ``bedrock.guardrail.trace`` in the merged config.

    Compliance-critical: guardrail trace observability must not be silently
    disabled by a typo.  Runs after the overlay merge, so it validates the
    effective value regardless of source (overlay or pre-existing on disk).

    - No bedrock/guardrail block, or no ``trace`` key → no-op (returns 0).
    - A valid value (case-insensitive) is normalized to upper-case in place.
    - Anything else → ERROR + return 1 so the pod crash-loops.
    """
    bedrock = cfg.get("bedrock")
    if not isinstance(bedrock, dict):
        return 0
    guardrail = bedrock.get("guardrail")
    if not isinstance(guardrail, dict):
        return 0
    if "trace" not in guardrail:
        return 0

    raw = guardrail["trace"]
    if raw is None:
        # Explicit null — treat as "no trace"; drop the key so the transport
        # falls back to its no-trace default rather than carrying a stale value.
        del guardrail["trace"]
        return 0

    normalized = str(raw).strip().upper()
    if normalized not in _VALID_GUARDRAIL_TRACE:
        print(
            f"[seed_admin_config] ERROR: bedrock.guardrail.trace={raw!r} is not "
            f"one of {sorted(_VALID_GUARDRAIL_TRACE)}; refusing to boot",
            file=sys.stderr,
        )
        return 1
    guardrail["trace"] = normalized
    return 0


def main() -> int:
    home = Path(os.environ.get("HERMES_HOME", "/opt/data"))
    config_path = home / "config.yaml"

    # Quick check: if no overlay file exists, this is a generic (non-pilot)
    # deploy.  Silent no-op so we don't churn config.yaml or log noise.
    overlay_path = Path(
        os.environ.get("HERMES_ADMIN_CONFIG_OVERLAY", _DEFAULT_OVERLAY_PATH)
    )
    if not overlay_path.exists():
        return 0

    cfg: dict = {}
    if config_path.exists():
        try:
            loaded = yaml.safe_load(config_path.read_text()) or {}
            if not isinstance(loaded, dict):
                print(
                    f"[seed_admin_config] WARNING: {config_path} did not parse as a "
                    f"mapping (got {type(loaded).__name__}); leaving file untouched",
                    file=sys.stderr,
                )
                return 0
            cfg = loaded
        except yaml.YAMLError as exc:
            # Don't make a broken config worse.  The user's startup will fail
            # for unrelated reasons anyway and they'll need to fix it by hand.
            print(
                f"[seed_admin_config] WARNING: {config_path} parse failed ({exc}); "
                f"leaving file untouched",
                file=sys.stderr,
            )
            return 0

    # Snapshot for idempotency check — only rewrite if something actually
    # changed.  Avoids needless mtime churn, which would invalidate
    # ``load_config()``'s in-process cache on every restart for no reason.
    before = copy.deepcopy(cfg)

    rc = _apply_overlay(cfg)
    if rc != 0:
        return rc

    # Compliance-critical post-merge validation.  Source-agnostic: fires on the
    # effective guardrail trace whether it came from the overlay or from disk.
    rc = _validate_guardrail_trace(cfg)
    if rc != 0:
        return rc

    if cfg == before:
        print(
            f"[seed_admin_config] config.yaml already matches overlay; no write"
        )
        return 0

    config_path.write_text(yaml.safe_dump(cfg, sort_keys=False))

    # Log what landed.  Use post-state so we report the effective values.
    summary_parts = []
    g = cfg.get("bedrock", {}).get("guardrail", {}) if isinstance(cfg.get("bedrock"), dict) else {}
    if g.get("guardrail_identifier"):
        summary_parts.append(
            f"guardrail(id={g.get('guardrail_identifier')!r}, "
            f"version={g.get('guardrail_version')!r}, "
            f"trace={g.get('trace')!r})"
        )
    m = cfg.get("model")
    if isinstance(m, dict):
        summary_parts.append(f"model={m.get('default')!r}")
    elif m:
        summary_parts.append(f"model={m!r}")
    print(
        f"[seed_admin_config] wrote {config_path}: {', '.join(summary_parts) or '(overlay applied)'}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
