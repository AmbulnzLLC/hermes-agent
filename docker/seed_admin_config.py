"""Seed admin-managed config keys at container boot.

Reads admin-controlled env vars and writes them into ``$HERMES_HOME/config.yaml``.
Designed for pilot/multi-tenant container deployments where the operator
provisions identity / model selection via Kubernetes injection rather than
baking it into the image.

Currently seeds:
- ``bedrock.guardrail.*`` from BEDROCK_GUARDRAIL_ID / BEDROCK_GUARDRAIL_VERSION
  / BEDROCK_GUARDRAIL_TRACE.
- top-level ``model`` from HERMES_MODEL.
- an arbitrary operator-supplied overlay deep-merged from the YAML file at
  ``HERMES_ADMIN_CONFIG_OVERLAY`` (default ``/config/overlay.yaml``).  This is
  the generic escape hatch: operators (e.g. the hermes-eks Helm chart) can pin
  *any* config key by mounting an overlay ConfigMap, with no code change here.

Behavior — guardrail:
- BEDROCK_GUARDRAIL_ID and BEDROCK_GUARDRAIL_VERSION must both be set and
  non-empty; otherwise the guardrail block is a no-op (no trace seeding either).
- BEDROCK_GUARDRAIL_TRACE is optional.  When set, it must be one of
  ``ENABLED``, ``ENABLED_FULL``, ``DISABLED`` (case-insensitive — normalized
  to upper-case on write).  Anything else is rejected with a non-zero exit
  so misconfigurations crash-loop rather than silently disabling tracing.

Behavior — model:
- HERMES_MODEL is optional.  When set and non-empty, the top-level ``model``
  key is overwritten with its value on every boot — admin wins, matching the
  guardrail mental model where Helm chart values are the source of truth.
- If the existing config.yaml already has a ``model`` value that differs from
  HERMES_MODEL, the script logs a WARNING (visible in the init-container
  output) before overwriting, so a user who did ``hermes config set model X``
  inside the pod has a breadcrumb explaining why their choice didn't survive
  the next restart.
- The key is always written as a top-level string (``model: <id>``), matching
  the shape produced by the Helm configmap.  Hermes itself also accepts the
  dict shape ``model: {default: <id>}`` (see cli.py); if config.yaml already
  has the dict shape, this script overwrites the ``default`` leaf and leaves
  sibling keys alone.

Behavior — overlay:
- ``HERMES_ADMIN_CONFIG_OVERLAY`` names a YAML file (default
  ``/config/overlay.yaml``).  If the file is absent or empty, the overlay step
  is a silent no-op.
- When present, its top-level mapping is **recursively deep-merged** over the
  existing config: nested mappings merge key-by-key; scalars, lists, and
  ``None`` in the overlay REPLACE whatever is at that path (lists are not
  concatenated).  The overlay is unrestricted — it can set any key.
- The overlay is applied AFTER the guardrail/model seeders, so an overlay entry
  wins over those when they touch the same path.  In practice keep the two
  mechanisms disjoint; the explicit seeders remain for backward-compat.
- A non-mapping or unparseable overlay file is logged and skipped (fail-open) —
  it does not clobber a working config.yaml.

General:
- Runs on every container boot.  Idempotent when env values already match.
- Preserves all other keys in config.yaml — only the keys above are touched.
  ``setdefault`` is used at every level so adjacent keys (e.g. ``bedrock.region``,
  top-level ``approvals``) are left untouched.
- Fails open: a pre-existing YAML parse error in config.yaml causes this
  script to log and exit 0 rather than clobbering the broken file.

This is a SEED, not a lock.  The user can subsequently edit, blank, or
remove these values via ``hermes config set`` or by editing config.yaml
directly; they will be re-seeded on the next container boot.  For guardrail
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
    doesn't apply.  (Contrast the guardrail path, where a bad trace value is
    compliance-critical and does crash.)
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


def _seed_guardrail(cfg: dict) -> int:
    """Apply BEDROCK_GUARDRAIL_* env into ``cfg`` in place.

    Returns 0 on success/no-op, 1 if BEDROCK_GUARDRAIL_TRACE is invalid.
    Mutates ``cfg`` only when env is set; returns silently otherwise.
    """
    guardrail_id = os.environ.get("BEDROCK_GUARDRAIL_ID", "").strip()
    guardrail_version = os.environ.get("BEDROCK_GUARDRAIL_VERSION", "").strip()
    guardrail_trace_raw = os.environ.get("BEDROCK_GUARDRAIL_TRACE", "").strip()

    if not (guardrail_id and guardrail_version):
        # Nothing to seed — this image build / deployment doesn't pin a
        # guardrail.  Silent no-op so generic (non-pilot) deploys aren't
        # noisy.
        return 0

    guardrail_trace: str | None = None
    if guardrail_trace_raw:
        normalized = guardrail_trace_raw.upper()
        valid = {"ENABLED", "ENABLED_FULL", "DISABLED"}
        if normalized not in valid:
            print(
                f"[seed_admin_config] ERROR: BEDROCK_GUARDRAIL_TRACE={guardrail_trace_raw!r} "
                f"is not one of {sorted(valid)}; refusing to boot",
                file=sys.stderr,
            )
            return 1
        guardrail_trace = normalized

    bedrock = cfg.setdefault("bedrock", {})
    if not isinstance(bedrock, dict):
        print(
            f"[seed_admin_config] WARNING: bedrock key in config.yaml is not a "
            f"mapping (got {type(bedrock).__name__}); skipping guardrail seed",
            file=sys.stderr,
        )
        return 0

    guardrail = bedrock.setdefault("guardrail", {})
    if not isinstance(guardrail, dict):
        print(
            f"[seed_admin_config] WARNING: bedrock.guardrail in config.yaml is not "
            f"a mapping (got {type(guardrail).__name__}); skipping guardrail seed",
            file=sys.stderr,
        )
        return 0

    guardrail["guardrail_identifier"] = guardrail_id
    guardrail["guardrail_version"] = guardrail_version
    if guardrail_trace is not None:
        guardrail["trace"] = guardrail_trace
    elif "trace" in guardrail:
        # Operator removed BEDROCK_GUARDRAIL_TRACE — clear the seeded value
        # so the transport falls back to its no-trace default rather than
        # carrying a stale setting forward.
        del guardrail["trace"]

    return 0


def _seed_model(cfg: dict) -> int:
    """Apply HERMES_MODEL env into ``cfg`` in place.

    HERMES_MODEL is optional.  When set, the top-level ``model`` key is
    overwritten — admin wins.  If the existing value differs, log a WARNING
    so users who edited it inside the pod have a breadcrumb.

    Returns 0 always (model misconfig is not fatal — guardrail compliance
    is the only crash-on-error case).
    """
    model = os.environ.get("HERMES_MODEL", "").strip()
    if not model:
        return 0

    existing = cfg.get("model")
    if isinstance(existing, dict):
        # User (or a previous Hermes write) chose the dict shape.  Update the
        # ``default`` leaf and leave siblings (``base_url``, ``provider``,
        # etc.) intact rather than collapsing the structure.
        prior = existing.get("default")
        if prior and prior != model:
            print(
                f"[seed_admin_config] WARNING: model.default in config.yaml "
                f"({prior!r}) differs from HERMES_MODEL ({model!r}); admin "
                f"value wins.  If a user changed it via 'hermes config set "
                f"model', that change won't survive a restart — update Helm "
                f"values instead.",
                file=sys.stderr,
            )
        existing["default"] = model
    else:
        # String shape (matches Helm configmap) or unset.
        if existing and existing != model:
            print(
                f"[seed_admin_config] WARNING: model in config.yaml ({existing!r}) "
                f"differs from HERMES_MODEL ({model!r}); admin value wins.  If "
                f"a user changed it via 'hermes config set model', that change "
                f"won't survive a restart — update Helm values instead.",
                file=sys.stderr,
            )
        cfg["model"] = model

    return 0


def main() -> int:
    home = Path(os.environ.get("HERMES_HOME", "/opt/data"))
    config_path = home / "config.yaml"

    # Quick check: if no admin env is set at all AND no overlay file exists,
    # this is a generic (non-pilot) deploy.  Silent no-op so we don't churn
    # config.yaml or log noise.
    has_guardrail_env = bool(
        os.environ.get("BEDROCK_GUARDRAIL_ID", "").strip()
        and os.environ.get("BEDROCK_GUARDRAIL_VERSION", "").strip()
    )
    has_model_env = bool(os.environ.get("HERMES_MODEL", "").strip())
    overlay_path = Path(
        os.environ.get("HERMES_ADMIN_CONFIG_OVERLAY", _DEFAULT_OVERLAY_PATH)
    )
    has_overlay = overlay_path.exists()
    if not (has_guardrail_env or has_model_env or has_overlay):
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

    rc = _seed_guardrail(cfg)
    if rc != 0:
        return rc

    rc = _seed_model(cfg)
    if rc != 0:
        return rc

    # Generic operator overlay — applied last so it can layer arbitrary keys
    # over (and win against) the explicit seeders above.  Fail-open: a bad
    # overlay is skipped, never crashes the boot.
    rc = _apply_overlay(cfg)
    if rc != 0:
        return rc

    if cfg == before:
        print(
            f"[seed_admin_config] config.yaml already matches admin env; no write"
        )
        return 0

    config_path.write_text(yaml.safe_dump(cfg, sort_keys=False))

    # Log what landed.  Use post-state to avoid leaking env values that
    # weren't actually applied (e.g. when guardrail block was rejected).
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
        f"[seed_admin_config] wrote {config_path}: {', '.join(summary_parts) or '(no changes)'}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
