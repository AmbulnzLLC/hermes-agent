"""Seed admin-managed config keys at container boot.

Reads BEDROCK_GUARDRAIL_ID and BEDROCK_GUARDRAIL_VERSION from the environment
and writes them into ``$HERMES_HOME/config.yaml`` under ``bedrock.guardrail``.
Designed for pilot/multi-tenant container deployments where the operator
provisions the guardrail identity via Kubernetes secrets / env injection
rather than baking it into the image.

Behavior:
- Both env vars must be set and non-empty; otherwise this script is a no-op.
- Runs on every container boot.  Idempotent when env values already match.
- Preserves all other keys in config.yaml — only the two guardrail leaves are
  touched.  ``setdefault`` is used at every level so adjacent keys under
  ``bedrock`` (e.g. ``region``, ``discovery``) are left untouched.
- Fails open: a pre-existing YAML parse error in config.yaml causes this
  script to log and exit 0 rather than clobbering the broken file.

This is a SEED, not a lock.  The user can subsequently edit, blank, or
remove these values via ``hermes config set`` or by editing config.yaml
directly; they will be re-seeded on the next container boot.  The actual
enforcement that every Bedrock call carries the guardrail must come from
the IAM layer (``bedrock:GuardrailIdentifier`` condition keys on the role
the pod assumes), not from this file.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml


def main() -> int:
    home = Path(os.environ.get("HERMES_HOME", "/opt/data"))
    config_path = home / "config.yaml"

    guardrail_id = os.environ.get("BEDROCK_GUARDRAIL_ID", "").strip()
    guardrail_version = os.environ.get("BEDROCK_GUARDRAIL_VERSION", "").strip()

    if not (guardrail_id and guardrail_version):
        # Nothing to seed — this image build / deployment doesn't pin a
        # guardrail.  Silent no-op so generic (non-pilot) deploys aren't
        # noisy.
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

    bedrock = cfg.setdefault("bedrock", {})
    if not isinstance(bedrock, dict):
        print(
            f"[seed_admin_config] WARNING: bedrock key in {config_path} is not a "
            f"mapping (got {type(bedrock).__name__}); leaving file untouched",
            file=sys.stderr,
        )
        return 0

    guardrail = bedrock.setdefault("guardrail", {})
    if not isinstance(guardrail, dict):
        print(
            f"[seed_admin_config] WARNING: bedrock.guardrail in {config_path} is not "
            f"a mapping (got {type(guardrail).__name__}); leaving file untouched",
            file=sys.stderr,
        )
        return 0

    # Only write if the values would change — avoids needless mtime churn,
    # which would invalidate ``load_config()``'s in-process cache on every
    # restart for no reason.
    existing_id = guardrail.get("guardrail_identifier")
    existing_version = guardrail.get("guardrail_version")
    if existing_id == guardrail_id and existing_version == guardrail_version:
        print(
            f"[seed_admin_config] bedrock.guardrail already matches env "
            f"(id={guardrail_id!r}, version={guardrail_version!r}); no write"
        )
        return 0

    guardrail["guardrail_identifier"] = guardrail_id
    guardrail["guardrail_version"] = guardrail_version

    config_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    print(
        f"[seed_admin_config] wrote bedrock.guardrail to {config_path} "
        f"(id={guardrail_id!r}, version={guardrail_version!r})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
