# AmbulnzLLC GitHub App Auth — Troubleshooting

Read this when something goes wrong with `scripts/get-token.sh` or the token it produces. SKILL.md links here by symptom — jump to the matching section.

## Symptom Index

| Symptom | Section |
|---------|---------|
| `git clone` / `git push` returns HTTP 401 | [§ Auth failures](#auth-failures) |
| API call returns HTTP 401 with a previously-working token | [§ Auth failures](#auth-failures) |
| API call returns HTTP 403 `Resource not accessible by integration` | [§ Scope gaps](#scope-gaps) |
| API call returns HTTP 404 on a repo you know exists | [§ Wrong org](#wrong-org) |
| `jwt.exceptions.InvalidKeyError: Could not parse the provided public key` | [§ Secret format](#secret-format) |
| `botocore.exceptions.ClientError: AccessDeniedException` from Secrets Manager | [§ AWS access](#aws-access) |
| Script prints nothing / hangs | [§ Network / dependencies](#network--dependencies) |
| Token works but cache file is missing | [§ Cache file](#cache-file) |

## Auth failures

**HTTP 401 from `api.github.com` or `git push/clone`:**

1. **Wrong git username.** GitHub requires the literal string `x-access-token` as the HTTPS username for App tokens. Anything else (the App slug, the bot name, blank) gets 401. Correct form:
   ```
   git clone https://x-access-token:${ITOK}@github.com/AmbulnzLLC/<repo>.git
   ```
2. **Stale cached token.** Tokens last ~1h. The script auto-refreshes when within 5 min of expiry, but if your shell captured an `ITOK` variable an hour ago, it's dead. Re-run the script to get a fresh one.
3. **Cache corruption.** If `~/.cache/ambulnz-gh-token.json` was edited or truncated, the script may hand back a malformed token. Fix:
   ```
   rm ~/.cache/ambulnz-gh-token.json
   ./scripts/get-token.sh
   ```
4. **Token leaked and revoked.** If the token starts with `ghs_` and was ever pushed to a public commit, GitHub's secret scanner revoked it. Re-mint and audit your shell history.

## Scope gaps

**HTTP 403 with body containing `"Resource not accessible by integration"`:**

This is **not** a token bug. The App lacks permission for that resource. The current installation has:

| Resource        | Level |
|-----------------|-------|
| `contents`      | write |
| `metadata`      | read  |
| `pull_requests` | write |

Anything outside that list — issues, Actions, secrets, admin, organization, members, packages, deployments, security events — will 403 forever, no matter how often you refresh the token.

**Don't loop retries.** Don't dig for workarounds. Escalate to expand the App's permissions, then re-mint.

## Wrong org

**HTTP 404 on a repo that exists:**

The installation only covers **AmbulnzLLC**. The App returns 404 (not 403) for repos in other orgs as a privacy measure — it can't even confirm they exist.

- For `hawknewton/...`, `nous-research/...`, etc. → use `github-auth` skill and a personal PAT instead.
- For a new AmbulnzLLC repo that just appeared 404 → the installation has `repository_selection: all`, so this is rare. Wait a minute and retry; GitHub propagation isn't instant.

## Secret format

**`jwt.exceptions.InvalidKeyError: Could not parse the provided public key`:**

The PEM in the secret has the wrong newline encoding. Two valid forms:

- Real `\n` bytes (preferred) — shows up as multiple lines in `repr()`.
- Literal `\n` two-byte sequences (legacy) — shows up as one long line in `repr()`.

The script normalizes both forms automatically. If you're still getting this error, the secret has a third, broken form (e.g. quote-escaped `\\n`, missing `-----BEGIN/-----END` markers, or a corrupted body). Inspect the raw secret:

```python
import boto3, json
sm = boto3.client('secretsmanager', region_name='us-west-2')
v = json.loads(sm.get_secret_value(
    SecretId='arn:aws:secretsmanager:us-west-2:854666668209:secret:vigo-github-app-key-3Wvs0z'
)['SecretString'])
pk = v['PRIVATE_KEY']
print('len:', len(pk))
print('contains real \\n:', '\n' in pk)
print('contains literal \\\\n:', '\\n' in pk)
print('has BEGIN marker:', '-----BEGIN' in pk)
print('first 60:', repr(pk[:60]))
```

If the markers are missing or the key body is mangled, the secret needs to be rotated. Coordinate with admin to recreate it from a fresh PEM file (`cat key.pem | jq -Rs .` produces correctly-escaped JSON content).

## AWS access

**`AccessDeniedException` or `UnrecognizedClientException` from Secrets Manager:**

The pod's IAM role doesn't have `secretsmanager:GetSecretValue` on the secret ARN. Confirm:

```bash
/opt/hermes/.venv/bin/python -c "
import boto3
sts = boto3.client('sts').get_caller_identity()
print(sts['Arn'])
"
```

Compare that ARN against the secret's resource policy (admin needs to check in the AWS console). If they don't match, this needs an admin-side IAM fix — the script can't work around it.

If the call fails with `NoCredentialsError`, the pod has no AWS credentials at all. That's an infrastructure issue (IRSA, instance profile, or env vars missing) — not something to retry.

## Network / dependencies

**Script prints nothing or hangs:**

1. **Missing Python deps.** The hermes venv (`/opt/hermes/.venv/bin/python`) has `boto3` and `PyJWT`. If you set `AMBULNZ_GH_PY` to a different interpreter, it must have both installed. Test:
   ```
   $AMBULNZ_GH_PY -c "import boto3, jwt; print('ok')"
   ```
2. **Network egress blocked.** The script needs to reach `secretsmanager.us-west-2.amazonaws.com` and `api.github.com`. Test:
   ```
   curl -sS -o /dev/null -w "%{http_code}\n" https://api.github.com
   curl -sS -o /dev/null -w "%{http_code}\n" https://secretsmanager.us-west-2.amazonaws.com
   ```
   Expect 200 and 400 respectively (400 from Secrets Manager because we're hitting the bare endpoint without an action — but 400 confirms reachability).
3. **DNS / proxy issue.** If `curl` itself fails, this is pod networking, not the script.

## Cache file

**Token works but `~/.cache/ambulnz-gh-token.json` doesn't exist:**

The script logs `warning: could not write cache file ...` to stderr if the cache directory isn't writable. The token is still printed (the script doesn't fail), but every invocation will mint a fresh one — wasting GitHub API quota and Secrets Manager calls.

Fix:
```bash
mkdir -p ~/.cache && chmod 700 ~/.cache
```

Or override the cache path:
```bash
AMBULNZ_GH_CACHE=/tmp/ambulnz-gh-token.json ./scripts/get-token.sh
```

(`/tmp` works for short sessions; the cache is wiped on pod restart, which is fine.)

## Configuration

The script reads two env vars:

- `AMBULNZ_GH_PY` — Python interpreter with `boto3` + `PyJWT`. Default: `/opt/hermes/.venv/bin/python`.
- `AMBULNZ_GH_CACHE` — cache file path. Default: `~/.cache/ambulnz-gh-token.json`.

The secret ARN, AWS region, and installation ID are hardcoded constants in the script. They change rarely; if they do, edit the script and update this file's *Scope gaps* table.

## Verification Checklist

After any change to the script or its environment:

- [ ] `scripts/get-token.sh` is executable (`chmod +x`)
- [ ] First invocation prints exactly one line starting with `ghs_` to stdout
- [ ] Diagnostics appear on stderr only (so `ITOK=$(...)` capture is clean)
- [ ] `curl -H "Authorization: Bearer <token>" https://api.github.com/repos/AmbulnzLLC/datalake-jobs` returns HTTP 200
- [ ] Second invocation reports `reusing cached token (expires in ...s)` on stderr
- [ ] `~/.cache/ambulnz-gh-token.json` exists with mode 0600
- [ ] Token never appears in tracked files, chat output, or commit messages
