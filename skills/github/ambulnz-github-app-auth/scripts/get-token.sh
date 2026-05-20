#!/usr/bin/env bash
# Mint (or reuse) a GitHub App installation token for AmbulnzLLC.
#
# Prints a live `ghs_...` token to stdout. Nothing else goes to stdout —
# diagnostics go to stderr — so callers can do:
#
#     ITOK=$(bash scripts/get-token.sh)
#     curl -H "Authorization: Bearer $ITOK" https://api.github.com/...
#     git clone https://x-access-token:${ITOK}@github.com/AmbulnzLLC/<repo>.git
#
# Caches the token at ~/.cache/ambulnz-gh-token.json. Re-uses it until within
# 5 minutes of `expires_at`, then mints a fresh one.
#
# Requires: boto3 + PyJWT in the Python interpreter. Defaults to the hermes
# venv at /opt/hermes/.venv/bin/python; override with $AMBULNZ_GH_PY.
set -euo pipefail

PY="${AMBULNZ_GH_PY:-/opt/hermes/.venv/bin/python}"
SECRET_ARN="arn:aws:secretsmanager:us-west-2:854666668209:secret:vigo-github-app-key-3Wvs0z"
INSTALLATION_ID="134007007"
CACHE_FILE="${AMBULNZ_GH_CACHE:-$HOME/.cache/ambulnz-gh-token.json}"

if [[ ! -x "$PY" ]]; then
    echo "ambulnz-gh-token: python interpreter not found at $PY" >&2
    echo "  set AMBULNZ_GH_PY to a python with boto3 + PyJWT installed" >&2
    exit 2
fi

mkdir -p "$(dirname "$CACHE_FILE")"

SECRET_ARN="$SECRET_ARN" \
INSTALLATION_ID="$INSTALLATION_ID" \
CACHE_FILE="$CACHE_FILE" \
exec "$PY" - <<'PYEOF'
"""Mint or reuse a GitHub App installation token. Stdout = token. Stderr = diagnostics."""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

SECRET_ARN = os.environ["SECRET_ARN"]
INSTALLATION_ID = os.environ["INSTALLATION_ID"]
CACHE_FILE = os.environ["CACHE_FILE"]

REFRESH_BEFORE_EXPIRY_S = 300  # refresh if <5 min remaining


def log(msg):
    print(f"ambulnz-gh-token: {msg}", file=sys.stderr)


def cached_token():
    """Return a still-valid cached token, or None."""
    try:
        with open(CACHE_FILE, "r") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    token = data.get("token")
    expires_at = data.get("expires_at")
    if not token or not expires_at:
        return None
    try:
        exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    remaining = (exp - datetime.now(timezone.utc)).total_seconds()
    if remaining < REFRESH_BEFORE_EXPIRY_S:
        log(f"cached token expires in {int(remaining)}s, refreshing")
        return None
    log(f"reusing cached token (expires in {int(remaining)}s)")
    return token


def mint_fresh_token():
    """Fetch secret, mint JWT, exchange for installation token."""
    try:
        import boto3  # noqa: F401
        import jwt    # PyJWT
    except ImportError as exc:
        log(f"missing dependency: {exc.name}. Install boto3 and PyJWT.")
        sys.exit(2)

    log("fetching App key from Secrets Manager")
    sm = boto3.client("secretsmanager", region_name="us-west-2")
    try:
        secret_string = sm.get_secret_value(SecretId=SECRET_ARN)["SecretString"]
    except Exception as exc:
        log(f"Secrets Manager error: {exc}")
        sys.exit(3)

    try:
        sec = json.loads(secret_string)
        client_id = sec["CLIENT_ID"]
        private_key = sec["PRIVATE_KEY"]
    except (json.JSONDecodeError, KeyError) as exc:
        log(f"secret malformed (expected JSON with CLIENT_ID + PRIVATE_KEY): {exc}")
        sys.exit(3)

    # Defensive: some secrets are stored with literal '\n' instead of real
    # newlines (depends on how the JSON was created). PyJWT requires real
    # newlines in the PEM, so normalize.
    if "\\n" in private_key and "\n" not in private_key:
        private_key = private_key.replace("\\n", "\n")

    now = int(time.time())
    app_jwt = jwt.encode(
        {"iat": now - 60, "exp": now + 540, "iss": client_id},
        private_key,
        algorithm="RS256",
    )

    log("exchanging JWT for installation token")
    req = urllib.request.Request(
        f"https://api.github.com/app/installations/{INSTALLATION_ID}/access_tokens",
        method="POST",
        headers={
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:500]
        log(f"GitHub returned HTTP {exc.code}: {body}")
        sys.exit(4)
    except urllib.error.URLError as exc:
        log(f"network error talking to api.github.com: {exc}")
        sys.exit(4)

    token = payload.get("token")
    expires_at = payload.get("expires_at")
    if not token or not expires_at:
        log(f"unexpected response shape: {payload}")
        sys.exit(4)

    # Cache. Owner-only perms — token is sensitive.
    try:
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"token": token, "expires_at": expires_at}, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, CACHE_FILE)
    except OSError as exc:
        log(f"warning: could not write cache file {CACHE_FILE}: {exc}")

    log(f"fresh token cached (expires_at={expires_at})")
    return token


def main():
    token = cached_token() or mint_fresh_token()
    print(token)


if __name__ == "__main__":
    main()
PYEOF
