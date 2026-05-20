---
name: ambulnz-github-app-auth
description: "Use when accessing AmbulnzLLC GitHub repos: run scripts/get-token.sh to mint a short-lived installation token from the Vigo (Agent) GitHub App."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [github, auth, aws, secrets-manager, ambulnz, github-app]
    related_skills: [github-auth, github-pr-workflow, github-repo-management]
---

# AmbulnzLLC GitHub App Auth

## Overview

For any work against `github.com/AmbulnzLLC/...` repos, authenticate as the **Vigo (Agent)** GitHub App, not as a personal user. A bundled script handles secret fetch, JWT signing, installation-token exchange, and ~1h caching with auto-refresh — you don't need to know the details to use it.

## When to Use

- Reading, writing, or cloning any repo under `github.com/AmbulnzLLC/...`
- Opening PRs, reading PR diffs, or pushing branches to AmbulnzLLC repos
- Calling the GitHub REST or GraphQL API for AmbulnzLLC resources

**Don't use for:** repos outside AmbulnzLLC, issues/Actions/secrets/admin operations (the App lacks those scopes), or personal repos — use `github-auth` instead.

## Usage

The script lives at `scripts/get-token.sh` next to this SKILL.md. It prints a live `ghs_...` token to stdout and nothing else, so it composes cleanly:

```bash
SKILL_DIR=~/workspace/hermes-agent/skills/github/ambulnz-github-app-auth
ITOK=$("$SKILL_DIR/scripts/get-token.sh")

# REST API
curl -sS -H "Authorization: Bearer $ITOK" \
     -H "Accept: application/vnd.github+json" \
     https://api.github.com/repos/AmbulnzLLC/datalake-jobs

# Git over HTTPS — username MUST be the literal string x-access-token
git clone https://x-access-token:${ITOK}@github.com/AmbulnzLLC/datalake-jobs.git

# gh CLI (without disturbing the pod's stored auth)
GH_TOKEN="$ITOK" gh repo view AmbulnzLLC/datalake-jobs
```

The script caches at `~/.cache/ambulnz-gh-token.json` (mode 0600). Subsequent calls return the cached token until within 5 minutes of expiry, then mint fresh. Safe to call on every operation.

## What the App Can Do

Installation `134007007` covers all AmbulnzLLC repos with these permissions:

| Resource        | Level |
|-----------------|-------|
| `contents`      | write |
| `metadata`      | read  |
| `pull_requests` | write |

Anything outside that — issues, Actions, secrets, admin — returns **HTTP 403 `Resource not accessible by integration`**. That's a permission gap, not a token problem; don't loop retries, escalate to expand the App.

## Configuration (rarely needed)

The script reads two env vars, both with sensible defaults:

- `AMBULNZ_GH_PY` — Python interpreter with `boto3` + `PyJWT`. Default: `/opt/hermes/.venv/bin/python`.
- `AMBULNZ_GH_CACHE` — cache file path. Default: `~/.cache/ambulnz-gh-token.json`.

The secret ARN, installation ID, and AWS region are hardcoded in the script (they change rarely; if they change, edit the script).

## Common Pitfalls

1. **Wrong git username for the token.** GitHub requires the literal username `x-access-token` for App tokens over HTTPS. Anything else (e.g. the App slug) gets a 401.

2. **Treating a 403 as a token failure.** HTTP 403 with `"Resource not accessible by integration"` means the App lacks that permission. The token is fine; the scope isn't there. Don't refresh — escalate.

3. **Logging or echoing the token.** Tokens prefixed `ghs_` are auto-revoked by GitHub secret scanning the moment they hit a public commit or log. Strip them from any output you persist or send back to chat.

4. **Trying it on non-AmbulnzLLC repos.** The installation only covers AmbulnzLLC. For anything else, the API returns 404 (looks like the repo doesn't exist) — use `github-auth` and a personal PAT instead.

5. **Editing the cache file by hand.** The script trusts `expires_at`. A stale or corrupted cache produces 401s on every call until you delete `~/.cache/ambulnz-gh-token.json`. When in doubt, `rm` it and retry.

6. **Secret stored with escaped newlines.** The JSON `PRIVATE_KEY` value may contain literal `\n` (two bytes) instead of real newlines, depending on how it was uploaded. The script normalizes both forms — but if you ever copy the PEM out of the secret manually, run it through `printf %b` or `sed 's/\\n/\n/g'` before feeding it to OpenSSL or PyJWT.

## Verification Checklist

- [ ] `scripts/get-token.sh` is executable (`chmod +x`)
- [ ] Running it prints exactly one line starting with `ghs_` to stdout, with diagnostics on stderr
- [ ] `curl -H "Authorization: Bearer <token>" https://api.github.com/repos/AmbulnzLLC/datalake-jobs` returns HTTP 200
- [ ] Second invocation reports `reusing cached token` on stderr
- [ ] `~/.cache/ambulnz-gh-token.json` exists with mode 0600
- [ ] Token is never written to a tracked file or chat output
