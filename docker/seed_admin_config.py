"""Seed admin-managed config keys at container boot.

Reads admin-controlled env vars and writes them into ``$HERMES_HOME/config.yaml``.
Designed for pilot/multi-tenant container deployments where the operator
provisions identity / model selection via Kubernetes injection rather than
baking it into the image.

Currently seeds:
- ``bedrock.guardrail.*`` from BEDROCK_GUARDRAIL_ID / BEDROCK_GUARDRAIL_VERSION
  / BEDROCK_GUARDRAIL_TRACE.
- top-level ``model`` from HERMES_MODEL.

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

    # Quick check: if no admin env is set at all, this is a generic (non-pilot)
    # deploy.  Silent no-op so we don't churn config.yaml or log noise.
    has_guardrail_env = bool(
        os.environ.get("BEDROCK_GUARDRAIL_ID", "").strip()
        and os.environ.get("BEDROCK_GUARDRAIL_VERSION", "").strip()
    )
    has_model_env = bool(os.environ.get("HERMES_MODEL", "").strip())
    if not (has_guardrail_env or has_model_env):
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
