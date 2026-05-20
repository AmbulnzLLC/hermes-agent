---
name: ambulnz-github-app-auth
description: "Use when accessing AmbulnzLLC GitHub repos: run scripts/get-token.sh to mint a short-lived installation token from the Vigo (Agent) GitHub App."
version: 1.1.0
author: Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [github, auth, aws, secrets-manager, ambulnz, github-app]
    related_skills: [github-auth, github-pr-workflow, github-repo-management]
---

# AmbulnzLLC GitHub App Auth

## When to Use

Any operation against `github.com/AmbulnzLLC/...` — REST API, GraphQL, `git clone`/`push`, `gh` CLI. Authenticate as the **Vigo (Agent)** GitHub App, not as a personal user.

**Don't use for:** repos outside AmbulnzLLC, issues/Actions/secrets/admin operations, or personal repos — use `github-auth` instead.

## How

The bundled script handles secret fetch, JWT signing, installation-token exchange, and ~1h caching with auto-refresh. Just run it:

```bash
SKILL_DIR=~/workspace/hermes-agent/skills/github/ambulnz-github-app-auth
ITOK=$("$SKILL_DIR/scripts/get-token.sh")

# REST API
curl -sS -H "Authorization: Bearer $ITOK" \
     -H "Accept: application/vnd.github+json" \
     https://api.github.com/repos/AmbulnzLLC/datalake-jobs

# Git over HTTPS — username MUST be the literal string x-access-token
git clone https://x-access-token:${ITOK}@github.com/AmbulnzLLC/datalake-jobs.git

# gh CLI without disturbing the pod's stored auth
GH_TOKEN="$ITOK" gh repo view AmbulnzLLC/datalake-jobs
```

Stdout = token only. Diagnostics on stderr. Safe to call on every operation; the cache prevents redundant minting.

## What the App Can Do

| Resource        | Level |
|-----------------|-------|
| `contents`      | write |
| `metadata`      | read  |
| `pull_requests` | write |

Anything else returns HTTP 403 — a permission gap, not a token problem. Don't retry; escalate to expand the App.

## When Things Go Wrong

Read **`references/troubleshooting.md`** — symptom-indexed. Quick guide:

- HTTP 401 on git or API → § Auth failures
- HTTP 403 `Resource not accessible by integration` → § Scope gaps
- HTTP 404 on a known repo → § Wrong org
- `InvalidKeyError: Could not parse the provided public key` → § Secret format
- `AccessDeniedException` from AWS → § AWS access
- Script hangs or prints nothing → § Network / dependencies
- Token works but cache is missing → § Cache file

The troubleshooting doc also covers configuration env vars (`AMBULNZ_GH_PY`, `AMBULNZ_GH_CACHE`) and the post-change verification checklist.

## Don't

- Log or echo the token. `ghs_` tokens are auto-revoked by GitHub secret scanning the moment they hit a public commit or log.
- Hand-edit `~/.cache/ambulnz-gh-token.json`. If it gets corrupted, just `rm` it.
- Retry on 403. The App lacks that scope; retrying won't change that.
