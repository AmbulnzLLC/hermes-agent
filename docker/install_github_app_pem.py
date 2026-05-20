"""Install the GitHub App private-key PEM at container boot.

Reads ``GITHUB_APP_PEM_SECRET_ID`` from the environment and, when set, fetches
the named AWS Secrets Manager secret, extracts the ``PRIVATE_KEY`` field, and
writes it to ``GITHUB_APP_PEM_DEST_PATH`` (default
``/opt/data/secrets/github-app.pem``) as ``hermes:hermes`` with mode ``0400``.

Designed for the AmbulnzLLC pilot (and any deployment that uses a GitHub App
for outbound git/GitHub API auth) where the operator provisions the App
credentials via AWS Secrets Manager rather than baking them into the image.
Once the PEM is on disk and the matching ``GITHUB_APP_*`` env vars are set,
Hermes's built-in ``GitHubAuth._try_github_app()`` (in ``tools/skills_hub.py``)
mints installation tokens automatically — no further wiring needed.

Behavior:
- ``GITHUB_APP_PEM_SECRET_ID`` unset or empty → silent no-op.  Generic
  (non-pilot) deploys aren't burdened.
- ``GITHUB_APP_PEM_SECRET_ID`` set → install is REQUIRED and any failure
  (missing ``boto3``, AWS API error, malformed secret JSON, missing
  ``PRIVATE_KEY`` field, write failure) crashes the boot with a non-zero
  exit code.  Loud > silent: a misconfigured pilot pod must crash-loop, not
  serve traffic without working GitHub auth.
- Secret payload contract: the secret value is a JSON object with at minimum
  a ``PRIVATE_KEY`` string field.  Newlines may be encoded as literal
  ``\\n`` two-byte sequences (the format produced by some console-pasted
  secret revisions); the script normalizes these to real newlines before
  writing.  Future rotations should prefer ``jq -Rs .`` or equivalent so
  the field already contains real newlines, but the normalization keeps
  legacy revisions working.
- Atomic write: the PEM is written to ``<dest>.tmp`` and ``os.replace``-d
  into place so a crash mid-write leaves the previous PEM intact.
- Idempotent: skips the write (and stat churn) when the destination already
  contains the same bytes.  Safe to run on every boot.
- Must run as root (the entrypoint root section, before the gosu drop) so
  the chown to ``hermes:hermes`` actually takes effect.

The PEM is written plaintext at rest.  Threat model: an attacker with shell
as ``hermes`` already has the pod's AWS instance/pod role and can fetch the
secret directly, so ``chmod 0400`` + per-pod filesystem isolation is
sufficient for the pilot.  Stronger protection (KMS-encrypted on-disk store,
in-memory-only key) is out of scope here.
"""
from __future__ import annotations

import json
import os
import pwd
import grp
import sys
from pathlib import Path


def _log(msg: str) -> None:
    print(f"[install_github_app_pem] {msg}", flush=True)


def _err(msg: str) -> None:
    print(f"[install_github_app_pem] ERROR: {msg}", file=sys.stderr, flush=True)


def main() -> int:
    secret_id = os.environ.get("GITHUB_APP_PEM_SECRET_ID", "").strip()
    if not secret_id:
        # Nothing to install — silent no-op.  Matches seed_admin_config.py
        # contract for env-driven optional provisioning.
        return 0

    dest_path = Path(
        os.environ.get(
            "GITHUB_APP_PEM_DEST_PATH", "/opt/data/secrets/github-app.pem"
        ).strip()
        or "/opt/data/secrets/github-app.pem"
    )
    region = os.environ.get("AWS_REGION", "").strip() or os.environ.get(
        "AWS_DEFAULT_REGION", ""
    ).strip()

    # boto3 lives in the venv (bedrock extra).  Entrypoint root section runs
    # before `source .venv/bin/activate`, so use the venv's site-packages
    # directly via the venv interpreter — the entrypoint invokes us with
    # /opt/hermes/.venv/bin/python3.
    try:
        import boto3  # noqa: F401  (imported for side-effect / availability check)
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError as exc:
        _err(
            f"boto3 not importable ({exc}); "
            "GITHUB_APP_PEM_SECRET_ID is set but the venv is missing the "
            "bedrock extra.  Rebuild the image with `uv sync --extra all`."
        )
        return 2

    client_kwargs = {}
    if region:
        client_kwargs["region_name"] = region
    client = boto3.client("secretsmanager", **client_kwargs)

    _log(f"fetching secret {secret_id!r}" + (f" in {region}" if region else ""))
    try:
        resp = client.get_secret_value(SecretId=secret_id)
    except (BotoCoreError, ClientError) as exc:
        _err(f"AWS get_secret_value failed: {exc}")
        return 3

    secret_string = resp.get("SecretString")
    if not secret_string:
        _err(
            "secret has no SecretString (binary secrets are not supported; "
            "store the PEM in a JSON envelope's PRIVATE_KEY field)"
        )
        return 4

    try:
        payload = json.loads(secret_string)
    except json.JSONDecodeError as exc:
        _err(
            f"secret value is not valid JSON ({exc}); expected an object "
            'with at least {"PRIVATE_KEY": "..."}'
        )
        return 5

    if not isinstance(payload, dict):
        _err("secret JSON is not an object")
        return 5

    private_key = payload.get("PRIVATE_KEY")
    if not isinstance(private_key, str) or not private_key.strip():
        _err(
            "secret JSON missing required string field 'PRIVATE_KEY' "
            "(or it is empty)"
        )
        return 6

    # Normalize literal \n two-byte sequences into real newlines.  Some
    # secret revisions get pasted through consoles that escape newlines this
    # way; PyJWT / cryptography then fail to parse the PEM.  No-op if the
    # secret already contains real newlines.
    if "\\n" in private_key and "\n" not in private_key:
        private_key = private_key.replace("\\n", "\n")

    # Sanity check: PEM should look like a PEM.  Catch obvious garbage early
    # rather than waiting for the first JWT mint to fail at runtime.
    if not private_key.lstrip().startswith("-----BEGIN"):
        _err(
            "PRIVATE_KEY does not start with '-----BEGIN' — secret value "
            "is not a PEM-formatted key"
        )
        return 7

    # Ensure trailing newline so OpenSSL is happy.
    if not private_key.endswith("\n"):
        private_key = private_key + "\n"

    pem_bytes = private_key.encode("utf-8")

    # Idempotency: skip the write if the destination already matches.  Mode
    # / ownership are still re-asserted below (cheap, and corrects drift).
    if dest_path.exists():
        try:
            if dest_path.read_bytes() == pem_bytes:
                _log(f"{dest_path} already current; re-asserting mode/owner")
                _enforce_mode_owner(dest_path)
                return 0
        except OSError as exc:
            _err(f"cannot read existing {dest_path}: {exc}")
            return 8

    dest_path.parent.mkdir(parents=True, exist_ok=True)

    tmp_path = dest_path.with_suffix(dest_path.suffix + ".tmp")
    try:
        # Write tmp with restrictive perms from the start.
        fd = os.open(
            tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
        )
        try:
            os.write(fd, pem_bytes)
        finally:
            os.close(fd)
        os.replace(tmp_path, dest_path)
    except OSError as exc:
        _err(f"failed to write {dest_path}: {exc}")
        # Clean up the half-written tmp if it survived.
        try:
            tmp_path.unlink()
        except OSError:
            pass
        return 9

    _enforce_mode_owner(dest_path)
    _log(f"installed PEM at {dest_path} (0400 hermes:hermes)")
    return 0


def _enforce_mode_owner(path: Path) -> None:
    """Set ``path`` to mode 0400 owned by hermes:hermes.

    Best-effort: a chown failure (e.g. rootless container, hermes user not
    yet created) is logged but does not fail the boot, since the more common
    failure mode is a permissions issue we can't fix from here anyway.  The
    PEM is unusable to anyone but root in that case, which is still secure.
    """
    try:
        os.chmod(path, 0o400)
    except OSError as exc:
        _err(f"chmod 0400 {path} failed: {exc}")
    try:
        uid = pwd.getpwnam("hermes").pw_uid
        gid = grp.getgrnam("hermes").gr_gid
        os.chown(path, uid, gid)
    except (KeyError, OSError) as exc:
        # KeyError: hermes user/group not present.  OSError: chown not
        # permitted (rootless).  Neither is fatal — the entrypoint's other
        # chowns (config.yaml, SOUL.md) tolerate the same condition.
        _log(f"chown hermes:hermes {path} skipped: {exc}")


if __name__ == "__main__":
    sys.exit(main())
