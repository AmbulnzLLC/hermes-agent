# Teams Outbound Files Implementation Plan

> **For Hermes:** Use `subagent-driven-development` skill to implement task-by-task.

**Goal:** Bring outbound file delivery to the existing `plugins/platforms/teams/adapter.py` so the agent can send documents/images/videos/audio to Teams users — completing the feature set inspired by upstream PR #13767, which #10/#1 deliberately scoped out.

**Architecture:** Outbound file delivery in Teams has two paths depending on conversation type. (1) **DMs** use the FileConsentCard flow — bot proposes an upload, Teams returns an `invoke` activity with an OneDrive upload URL when the user accepts, bot PUTs the bytes, then sends a follow-up `FileInfoCard` so the file renders as a first-class attachment. (2) **Channels and group chats** require Microsoft Graph: bot uploads to the team's SharePoint drive, then attaches a `file.download.info` card referencing the drive item. Both paths are plumbed through new outbound methods on the existing adapter; the FileConsent flow needs an `invoke` handler and pending-upload bookkeeping; channel uploads need a Graph client. Inbound hosted-content fallback (Graph download) is a third smaller piece for inbound resilience.

**Tech Stack:**
- Existing: `microsoft-teams-apps` SDK (already wired into adapter), `aiohttp`, plugin-loader infra under `plugins/platforms/teams/`
- New deps: `msgraph-sdk` (Graph client), `azure-identity` and/or `msal` (Graph token acquisition), `aiofiles` (already in tree, verify)
- Reference (do **not** copy verbatim — different SDK base): `gateway/platforms/msteams/{cards,graph,auth,adapter}.py` on branch `pr-13767`

**Repo:** `AmbulnzLLC/hermes-agent`, branch off `main`, target branch name `feat/teams-outbound-files`

**SDK reality check:** The plugin adapter uses Microsoft's **new** `microsoft-teams-apps` SDK. PR #13767 uses the **legacy** `botbuilder-core` + `msgraph-sdk` stack. **Auth, activity dispatch, and outbound APIs are different.** This plan ports the *behaviour and shape* from #13767, not the source. Token acquisition for Graph specifically still uses MSAL/azure-identity because `microsoft-teams-apps` only handles Bot Framework auth, not Graph.

---

## Tasks

### Task 1: Branch + skeleton modules

**Objective:** Create the branch and three empty stub modules so subsequent tasks have a known landing place.

**Files:**
- Create: `plugins/platforms/teams/cards.py` (empty stub with module docstring + `__all__ = []`)
- Create: `plugins/platforms/teams/graph.py` (empty stub)
- Create: `plugins/platforms/teams/auth_graph.py` (empty stub — `auth_graph` not `auth` to avoid colliding with whatever the Teams SDK uses)

**Step 1: Cut branch**

```bash
cd ~/workspace/hermes-agent
git fetch origin
git checkout -b feat/teams-outbound-files origin/main
```

**Step 2: Create stub files**

Each file gets just a header so imports resolve once tasks 3–6 land:

```python
"""<one-line module purpose>"""
from __future__ import annotations
__all__: list[str] = []
```

**Step 3: Commit**

```bash
git add plugins/platforms/teams/cards.py plugins/platforms/teams/graph.py plugins/platforms/teams/auth_graph.py
git commit -m "feat(teams): scaffold cards/graph/auth_graph modules for outbound files"
```

---

### Task 2: Add Graph dependencies via lazy_deps

**Objective:** Register `msgraph-sdk`, `msgraph-core`, `azure-identity`, and `msal` under the existing `lazy_deps` mechanism so they install on first import without polluting the base wheel.

**Files:**
- Modify: `tools/lazy_deps.py` — add `platform.teams.graph` entry alongside the existing `platform.teams` entry (find via `git grep -n "platform.teams" tools/lazy_deps.py`)
- Modify: `pyproject.toml` — extend the existing `teams` extra to include the new packages, OR add a new `teams-files` extra (decide in Task 1's review)

**Step 1: Check current lazy_deps structure**

```bash
git grep -n "platform.teams\|teams" tools/lazy_deps.py | head
```

**Step 2: Add the entry**

> **Pin exactly, no ranges.** Both `tools/lazy_deps.py` and `pyproject.toml` carry an explicit no-ranges policy (post-Mini-Shai-Hulud, 2026-05-12) — every other entry in `LAZY_DEPS` uses `==`. Discover the latest stable PyPI versions before committing (`pip index versions <pkg>` or `curl https://pypi.org/pypi/<pkg>/json`) and pin exactly. These are auth/identity packages — high-value supply-chain target — so the policy applies more strongly here, not less.

Pattern (exact form depends on what the file looks like — read first; versions below are 2026-05-14 latest stable, refresh):

```python
"platform.teams.graph": (
    "msgraph-sdk==1.57.0",
    "msgraph-core==1.3.8",
    "azure-identity==1.25.3",
    "msal==1.36.0",
),
```

**Step 3: Add the extra to pyproject**

```toml
[project.optional-dependencies]
# Pinned exactly per the policy block at the top of this file.
# msal is kept as an explicit direct pin even though azure-identity
# pulls it transitively — Task 4 imports msal.ConfidentialClientApplication
# directly, so the dep is direct in source and should be direct in metadata.
teams-files = [
    "msgraph-sdk==1.57.0",
    "msgraph-core==1.3.8",
    "azure-identity==1.25.3",
    "msal==1.36.0",
]
```

Add `"teams-files"` to the `all` aggregator extra if one exists.

**Step 4: Verify**

```bash
python -c "from tools.lazy_deps import REGISTRATIONS; assert 'platform.teams.graph' in REGISTRATIONS"
pip install -e '.[teams-files]'
python -c "import msgraph, azure.identity, msal"
```

**Step 5: Commit**

```bash
git add tools/lazy_deps.py pyproject.toml
git commit -m "feat(teams): register Graph deps under lazy_deps + teams-files extra"
```

---

### Task 3: Card builders (cards.py)

**Objective:** Build the three cards needed for outbound files: `FileConsentCard` (DM upload kickoff), `FileInfoCard` (post-upload attachment render), `FileDownloadCard` (channel/group upload reference). Pure functions — no I/O, no SDK calls.

**Files:**
- Modify: `plugins/platforms/teams/cards.py`
- Create: `tests/plugins/platforms/teams/test_cards.py`

**Reference (read, then translate):** `git show pr-13767:gateway/platforms/msteams/cards.py` lines 211–304.

**Step 1: Write failing tests**

```python
# tests/plugins/platforms/teams/test_cards.py
from plugins.platforms.teams.cards import (
    build_file_consent_card,
    build_file_info_card,
    build_file_download_card,
)

def test_file_consent_card_has_correct_content_type():
    card = build_file_consent_card("foo.pdf", size_bytes=1234, accept_context={"upload_id": "u1"})
    assert card["contentType"] == "application/vnd.microsoft.teams.card.file.consent"
    assert card["content"]["name"] == "foo.pdf"
    assert card["content"]["sizeInBytes"] == 1234
    assert card["content"]["acceptContext"] == {"upload_id": "u1"}

def test_file_consent_card_seeds_upload_id_when_missing():
    card = build_file_consent_card("foo.pdf", size_bytes=1, accept_context={})
    assert "upload_id" in card["content"]["acceptContext"]

def test_file_info_card_uses_correct_content_type():
    card = build_file_info_card("foo.pdf", file_type="pdf", url="https://...")
    assert card["contentType"] == "application/vnd.microsoft.teams.card.file.info"
    assert card["contentUrl"] == "https://..."

def test_file_download_card_uses_download_info_content_type():
    card = build_file_download_card(
        unique_id="aaaa-bbbb",
        file_type="pdf",
        url="https://contoso.sharepoint.com/...",
    )
    assert card["contentType"] == "application/vnd.microsoft.teams.file.download.info"
    assert card["content"]["uniqueId"] == "aaaa-bbbb"
```

**Step 2: Run, verify red**

```bash
pytest tests/plugins/platforms/teams/test_cards.py -v
```

Expected: 4 failures (`ImportError` or `AttributeError`).

**Step 3: Implement**

```python
# plugins/platforms/teams/cards.py
"""Teams card builders for file delivery flows.

These are pure dict factories. They emit the JSON shapes the Teams Bot
Framework recognizes; no SDK objects involved so they work with any
Teams transport layer.

References:
- FileConsentCard: https://learn.microsoft.com/microsoftteams/platform/bots/how-to/bots-files
- FileInfoCard:    same doc, "Notify the user about the uploaded file"
- file.download.info attachment: channel / group upload references
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

FILE_CONSENT_CONTENT_TYPE = "application/vnd.microsoft.teams.card.file.consent"
FILE_INFO_CONTENT_TYPE = "application/vnd.microsoft.teams.card.file.info"
FILE_DOWNLOAD_INFO_CONTENT_TYPE = "application/vnd.microsoft.teams.file.download.info"


def build_file_consent_card(
    filename: str,
    size_bytes: int,
    accept_context: Optional[Dict[str, Any]] = None,
    description: str = "",
) -> Dict[str, Any]:
    """Build the FileConsentCard attachment that kicks off an outbound DM upload.

    The user's accept/decline triggers a ``fileConsent/invoke`` activity;
    on accept the payload includes ``uploadInfo`` with a OneDrive URL the
    bot PUTs the bytes to. ``acceptContext`` is echoed back unchanged so
    the bot can correlate the invoke with the original upload request.
    """
    ctx = dict(accept_context or {})
    ctx.setdefault("upload_id", str(uuid.uuid4()))
    return {
        "contentType": FILE_CONSENT_CONTENT_TYPE,
        "name": filename,
        "content": {
            "description": description,
            "sizeInBytes": int(size_bytes),
            "acceptContext": ctx,
            "declineContext": ctx,
        },
    }


def build_file_info_card(filename: str, file_type: str, url: str) -> Dict[str, Any]:
    """Build the FileInfoCard the bot sends *after* a successful FileConsent upload.

    Without this, the file does not render as a native attachment in the
    DM — the user just sees the consent card flip to 'uploaded'.
    """
    return {
        "contentType": FILE_INFO_CONTENT_TYPE,
        "contentUrl": url,
        "name": filename,
        "content": {
            "uniqueId": str(uuid.uuid4()),
            "fileType": file_type,
        },
    }


def build_file_download_card(unique_id: str, file_type: str, url: str) -> Dict[str, Any]:
    """Build a file.download.info attachment for channel / group uploads.

    Used after the bot has uploaded to SharePoint via Graph and has the
    drive item's webUrl. Teams clients render this as a downloadable
    attachment in channel posts.
    """
    return {
        "contentType": FILE_DOWNLOAD_INFO_CONTENT_TYPE,
        "contentUrl": url,
        "name": unique_id,
        "content": {
            "uniqueId": unique_id,
            "fileType": file_type,
        },
    }
```

Update `__all__` to export the three.

**Step 4: Run, verify green**

```bash
pytest tests/plugins/platforms/teams/test_cards.py -v
```

Expected: 4 passed.

**Step 5: Commit**

```bash
git add plugins/platforms/teams/cards.py tests/plugins/platforms/teams/test_cards.py
git commit -m "feat(teams): card builders for FileConsent/FileInfo/FileDownload"
```

---

### Task 4: Graph token provider (auth_graph.py)

**Objective:** Acquire a Microsoft Graph access token using the same `TEAMS_CLIENT_ID` / `TEAMS_CLIENT_SECRET` / `TEAMS_TENANT_ID` already required by the plugin. Cache by scope, refresh near expiry, asyncio-safe.

**Files:**
- Modify: `plugins/platforms/teams/auth_graph.py`
- Create: `tests/plugins/platforms/teams/test_auth_graph.py`

**Reference:** `git show pr-13767:gateway/platforms/msteams/auth.py` lines 84–172 (`_MsalBackedProvider`, `SecretCredentialProvider`).

**Step 1: Tests**

```python
# tests/plugins/platforms/teams/test_auth_graph.py
import asyncio
import pytest
from unittest.mock import patch

from plugins.platforms.teams.auth_graph import GraphTokenProvider, AuthError

@pytest.mark.asyncio
async def test_get_token_caches_per_scope(monkeypatch):
    calls = []

    class FakeMsal:
        def acquire_token_for_client(self, scopes):
            calls.append(tuple(scopes))
            return {"access_token": "tok", "expires_in": 3600}

    p = GraphTokenProvider("app", "tenant", "secret")
    with patch.object(p, "_build_msal_app", return_value=FakeMsal()):
        a = await p.get_token("https://graph.microsoft.com/.default")
        b = await p.get_token("https://graph.microsoft.com/.default")
    assert a == "tok" and b == "tok"
    assert len(calls) == 1  # cached

@pytest.mark.asyncio
async def test_get_token_raises_on_msal_error():
    class FakeMsal:
        def acquire_token_for_client(self, scopes):
            return {"error": "invalid_client", "error_description": "bad"}

    p = GraphTokenProvider("app", "tenant", "secret")
    with patch.object(p, "_build_msal_app", return_value=FakeMsal()):
        with pytest.raises(AuthError):
            await p.get_token("https://graph.microsoft.com/.default")
```

**Step 2: Run, verify red**

```bash
pytest tests/plugins/platforms/teams/test_auth_graph.py -v
```

**Step 3: Implement** — write `auth_graph.py` with `AuthError`, `GraphTokenProvider` (asyncio.Lock per scope, MSAL ConfidentialClientApplication, expiry-aware caching). Pattern from #13767 `_MsalBackedProvider` but trimmed to confidential-client only (no certificate, no managed identity in v1).

**Step 4: Run, verify green**

```bash
pytest tests/plugins/platforms/teams/test_auth_graph.py -v
```

**Step 5: Commit**

```bash
git add plugins/platforms/teams/auth_graph.py tests/plugins/platforms/teams/test_auth_graph.py
git commit -m "feat(teams): MSAL-backed Graph token provider with per-scope caching"
```

---

### Task 5: Graph client (graph.py) — upload + download

**Objective:** Wrap `msgraph-sdk` for the two operations we actually need: `download_hosted_content` (inbound fallback) and `upload_to_sharepoint` (channel/group outbound). Everything else from #13767's GraphClient (channel listing, user resolution) is YAGNI for v1.

**Files:**
- Modify: `plugins/platforms/teams/graph.py`
- Create: `tests/plugins/platforms/teams/test_graph.py`

**Reference:** `git show pr-13767:gateway/platforms/msteams/graph.py` lines 277–346.

**Step 1: Tests** (mock the GraphServiceClient at the boundary; don't test the SDK)

```python
@pytest.mark.asyncio
async def test_download_hosted_content_returns_bytes():
    # mock client.teams.by_team_id(...).channels...etc to return bytes
    ...

@pytest.mark.asyncio
async def test_upload_to_sharepoint_returns_drive_item():
    # mock client.drives.by_drive_id(...).items.by_drive_item_id(...).content.put
    ...
```

**Step 2–4: Red, implement, green** — follow #13767's signatures, adapted to be lazily-imported (msgraph not in base extra).

**Step 5: Commit**

```bash
git commit -m "feat(teams): Graph client — upload_to_sharepoint + download_hosted_content"
```

---

### Task 6: Adapter — outbound `send_document` / `send_video` / `send_voice`

**Objective:** Add three outbound methods on the existing `TeamsAdapter` class that route to the right path (FileConsent for DMs, Graph upload for channels) based on conversation type.

**Files:**
- Modify: `plugins/platforms/teams/adapter.py` — extend `TeamsAdapter` class
- Create/Modify: `tests/plugins/platforms/teams/test_adapter_outbound.py`

**Step 1: Tests** — three test classes, one per method, mocking the SDK send + the new helpers.

**Step 2–4: TDD cycle** — `send_document(chat_id, path, caption=None)`:

```python
async def send_document(self, chat_id, path, caption=None):
    convo_type = self._conversation_type_for(chat_id)
    if convo_type == "personal":
        await self._send_via_file_consent(chat_id, path, caption)
    else:
        await self._send_via_graph_upload(chat_id, path, caption)
```

Helper bodies wire up Task 3's cards + Task 5's Graph client. Pending-upload bookkeeping lives on the adapter as `self._pending_uploads: dict[upload_id, PendingUpload]`.

**Step 5: Commit**

```bash
git commit -m "feat(teams): outbound send_document/send_video/send_voice with DM-vs-channel dispatch"
```

---

### Task 7: Adapter — `fileConsent/invoke` handler

**Objective:** When the user accepts a FileConsentCard, Teams sends an `invoke` activity with `name=fileConsent/invoke`. The adapter must consume the upload URL, PUT the bytes, then send a `FileInfoCard` follow-up.

**Files:**
- Modify: `plugins/platforms/teams/adapter.py` — register the invoke handler
- Modify: `tests/plugins/platforms/teams/test_adapter_outbound.py`

**Step 1: Test** — simulate the invoke activity, assert PUT happens to the right URL with correct bytes, assert FileInfoCard is sent.

**Step 2–4: TDD** — register via `App.on_invoke("fileConsent/invoke")` (verify exact API name in the SDK), look up pending upload by `upload_id` from `acceptContext`, PUT bytes via `aiohttp`, send FileInfoCard.

Edge cases the test must cover:
- `action == "decline"` → drop pending entry, no PUT
- `upload_id` not in `_pending_uploads` → 404 invoke response
- PUT returns non-2xx → log + 500 invoke response, drop pending entry

**Step 5: Commit**

```bash
git commit -m "feat(teams): handle fileConsent/invoke — PUT bytes + FileInfoCard follow-up"
```

---

### Task 8: Inbound Graph fallback for hosted content

**Objective:** When an inbound attachment is hosted-content (no `download.info`, no `contentUrl`), fall back to Graph's `downloadHostedContent` API. This is the third inbound shape #10 didn't cover.

**Files:**
- Modify: `plugins/platforms/teams/adapter.py` — extend the existing inbound-attachment dispatcher
- Modify: `tests/plugins/platforms/teams/test_adapter_inbound.py` (or wherever the existing `[teams][attach]` tests live — `git grep` first)

**Step 1: Test** — synthesize an activity with a hosted-content attachment, assert `GraphClient.download_hosted_content` is called and the cached path is returned.

**Step 2–4: TDD** — the existing dispatcher in `adapter.py` (commit `f06aef05b`) needs a new branch *after* the `image/audio/video/file.download.info` branches — final fallback when `content.uniqueId` is present.

**Step 5: Commit**

```bash
git commit -m "feat(teams): Graph fallback for inbound hosted-content attachments"
```

---

### Task 9: plugin.yaml — declare optional Graph env if any

**Objective:** Surface any new optional env vars (only if v1 introduces them — likely none, since Graph reuses `TEAMS_CLIENT_*`).

**Files:**
- Modify: `plugins/platforms/teams/plugin.yaml`

If no new env: skip this task. Otherwise add to `optional_env`.

**Commit:**

```bash
git commit -m "feat(teams): document new optional env for Graph (if any)"
```

---

### Task 10: Manual end-to-end smoke test

**Objective:** Verify on the `hermes-docgo` single-user pod (or whatever single-user environment you have running with the feature flagged in) that:

1. Bot says "make me a PDF" → bot generates → bot sends FileConsentCard
2. User clicks Accept → file lands in OneDrive → FileInfoCard shows up rendered
3. Repeat in a channel → file shows up as native channel attachment
4. User uploads a hosted-content attachment in a channel → bot can read it

**Files:** none — this is operational verification.

**Output:** A short paste of the verification log into the PR description's "Validation" section.

---

### Task 11: PR

**Objective:** Open the PR.

**Steps:**

```bash
git push -u origin feat/teams-outbound-files
~/.local/bin/gh pr create --repo AmbulnzLLC/hermes-agent \
  --base main \
  --title "feat(teams): outbound files — FileConsent + Graph upload + hosted-content fallback" \
  --body-file docs/plans/2026-05-14-teams-outbound-files.md
```

(Or write a tighter `--body` summarizing the plan rather than dumping it whole.)

---

## Out of scope (intentionally — punt to v2)

- Adaptive Card / Poll builders from #13767's `cards.py` (orthogonal feature, not file-related)
- Channel listing, user resolution, joined-teams discovery from #13767's `GraphClient` (no consumer yet)
- Certificate-based and managed-identity Graph auth (`SecretCredentialProvider` is enough for the pilot)
- Standalone-send / cron outbound delivery of files (text-only via cron is enough for v1)
- Retry/backoff on Graph 429s — surface the error, let the agent retry at the application layer

## Things review should push on

1. **SDK split brain.** Teams Bot Framework auth via `microsoft-teams-apps`, Graph auth via MSAL. Two token providers in one adapter. Acceptable, or ugly enough to want a unifying wrapper now?
2. **Pending-upload bookkeeping is in-process dict.** Crashes lose pending uploads. For pilot scale (≤10 users, ≤1 file in flight per user) fine. Persistence belongs in v2.
3. **Test surface for the Graph client** is mocked at the SDK boundary. Real-Graph integration tests would need a test tenant — defer to a manual smoke test (Task 10).
4. **Channel/group send dispatch** uses `conversationType == "personal"` to pick the path. If `groupChat` should go through FileConsent (not Graph), that's a one-line change but worth deciding before implementation.
