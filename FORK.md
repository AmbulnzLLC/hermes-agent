# AmbulnzLLC fork — branch notes

This is AmbulnzLLC's fork of
[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent).

## `msteams-v2`

The branch AmbulnzLLC deployments build from for Microsoft Teams file support.

**Contents:** upstream **PR #13767** ("Microsoft Teams platform adapter V2"),
unmodified — based on commit `eea44d7c1c594fa3490cb11660b7510e72208783` (the PR
head). It adds the built-in `gateway/platforms/msteams/` package: inbound media
plus outbound file delivery via the Teams FileConsentCard flow.

The only thing this branch adds on top of upstream is this `FORK.md`.

> Note: `msteams-v2` is built **directly on upstream #13767**. It does *not*
> include AmbulnzLLC's `main`-branch customizations
> (`feat/ambulnz-platform-customizations`) — it is a deliberately narrow,
> single-purpose branch.

**Consumed by:**
[`AmbulnzLLC/hermes-eks`](https://github.com/AmbulnzLLC/hermes-eks) — its
`build-push.sh` pins this branch **by commit SHA** (not by branch name) so image
builds stay reproducible.

### Why there is no `bedrock-lazy-deps` patch

`hermes-eks` previously carried a `patches/0001-bedrock-lazy-deps.patch` that
lazy-installed `boto3` via `tools/lazy_deps.py`. It is **deliberately not** folded
into this branch — on the #13767 base it is both inert and unnecessary:

- `tools/lazy_deps.py` does not exist at `eea44d7c`, so the patch's import would
  just hit its own `try/except` and do nothing.
- `pyproject.toml` at `eea44d7c` still has `boto3` in the `[bedrock]` extra, and
  `[all]` — which the Dockerfile installs (`uv pip install -e ".[all]"`) —
  includes `[bedrock]`. So `boto3` is already baked into the image at build time.

That patch was written for a newer upstream state (the `lazy_deps` mechanism plus
the removal of `boto3` from `[all]` — upstream PRs #24220 / #24515), which #13767
predates. If `msteams-v2` is ever rebased onto a base that *has* dropped `boto3`
from `[all]`, the lazy-install behaviour will need revisiting then.

## Re-syncing when upstream #13767 moves or merges

`#13767` is an open upstream PR. If it is updated, or merges into upstream `main`:

1. Fetch the new upstream state — `git fetch upstream pull/13767/head`, or
   `git fetch upstream main` once it has merged.
2. Reset `msteams-v2` to the new upstream point and re-add this `FORK.md` commit
   (cherry-pick it).
3. Force-push `msteams-v2`, then bump `HERMES_REF` in `hermes-eks/build-push.sh`
   to the new HEAD SHA.

Once #13767 is merged upstream and lands in a tagged release, this branch can be
retired in favour of a plain upstream pin.
