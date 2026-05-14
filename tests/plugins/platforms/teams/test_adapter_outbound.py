"""Tests for outbound file methods on TeamsAdapter (Task 6).

Covers:

* Public ``send_document`` / ``send_video`` / ``send_voice``.
* DM dispatch via FileConsent card + ``_pending_uploads`` bookkeeping.
* Channel dispatch via Graph SharePoint upload + FileDownload card.
* ``_is_channel_chat`` heuristic — conv_ref override beats id-shape.
* ``_send_attachment`` activity_sender vs send selection on conv_ref presence.

The fixture builds a ``TeamsAdapter`` *without* calling ``connect()`` so we
do not need the real SDK app, aiohttp listener, or MSAL credentials. The
``self._app`` slot is filled with an ``AsyncMock`` whose ``send`` and
``activity_sender.send`` methods record their arguments.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import SendResult
from plugins.platforms.teams.adapter import TeamsAdapter
from plugins.platforms.teams.cards import (
    FILE_CONSENT_CONTENT_TYPE,
    FILE_DOWNLOAD_INFO_CONTENT_TYPE,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def adapter(monkeypatch) -> TeamsAdapter:
    """Build a TeamsAdapter with mocked SDK app, no live network."""
    # Make sure stale env vars don't seep into _sharepoint_site_id / etc.
    for var in (
        "TEAMS_CLIENT_ID",
        "TEAMS_CLIENT_SECRET",
        "TEAMS_TENANT_ID",
        "TEAMS_SHAREPOINT_SITE_ID",
        "TEAMS_SHAREPOINT_FOLDER",
    ):
        monkeypatch.delenv(var, raising=False)

    cfg = PlatformConfig(
        enabled=True,
        extra={
            "client_id": "fake-client",
            "client_secret": "fake-secret",
            "tenant_id": "fake-tenant",
        },
    )
    a = TeamsAdapter(cfg)

    # Replace the SDK App with a Mock that has the two send entry points
    # the adapter dispatches through.
    fake_app = MagicMock()
    fake_app.send = AsyncMock(return_value=Mock(id="sent-msg-id"))
    fake_app.activity_sender = MagicMock()
    fake_app.activity_sender.send = AsyncMock(return_value=Mock(id="conv-msg-id"))
    a._app = fake_app
    return a


@pytest.fixture
def doc_file(tmp_path: Path) -> Path:
    p = tmp_path / "doc.pdf"
    p.write_bytes(b"X" * 100)
    return p


# ---------------------------------------------------------------------------
# Public send_* surface — input validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_document_missing_file_returns_failure(adapter):
    result = await adapter.send_document("19:foo@unq.gbl.spaces", "/nonexistent/file.txt")
    assert result.success is False
    assert result.retryable is False
    assert "not found" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_send_document_unreadable_file_returns_failure(adapter, tmp_path):
    # Pass a directory path — open() on it raises IsADirectoryError.
    bad = tmp_path / "as_dir"
    bad.mkdir()
    result = await adapter.send_document("19:foo@unq.gbl.spaces", str(bad))
    assert result.success is False
    # "not found" path treats directories as non-files; either way it's a hard fail.
    assert result.retryable is False


# ---------------------------------------------------------------------------
# DM (FileConsent) dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_document_to_dm_uses_file_consent_card(adapter, doc_file):
    chat_id = "a:dm-thread-id"  # DM-shaped — no @thread.
    result = await adapter.send_document(chat_id, str(doc_file))

    assert result.success is True
    # The send happened via _app.send (no conv_ref stored).
    assert adapter._app.send.await_count == 1
    activity = adapter._app.send.await_args.args[1]
    # The Attachment carries the FileConsentCard contentType.
    atts = activity.attachments
    assert len(atts) == 1
    assert atts[0].content_type == FILE_CONSENT_CONTENT_TYPE
    # Pending uploads bookkeeping populated.
    assert len(adapter._pending_uploads) == 1
    upload_id, payload = next(iter(adapter._pending_uploads.items()))
    assert payload["filename"] == "doc.pdf"
    assert payload["bytes"] == b"X" * 100
    assert payload["chat_id"] == chat_id


@pytest.mark.asyncio
async def test_pending_upload_recorded_with_correct_payload(adapter, doc_file):
    chat_id = "a:dm-thread-id"
    await adapter.send_document(
        chat_id, str(doc_file), caption="hello there", reply_to="parent-msg-1"
    )

    assert len(adapter._pending_uploads) == 1
    payload = next(iter(adapter._pending_uploads.values()))
    assert set(payload.keys()) >= {"filename", "bytes", "chat_id", "caption", "reply_to"}
    assert payload["filename"] == "doc.pdf"
    assert payload["bytes"] == b"X" * 100
    assert payload["chat_id"] == chat_id
    assert payload["caption"] == "hello there"
    assert payload["reply_to"] == "parent-msg-1"


@pytest.mark.asyncio
async def test_send_local_file_uses_basename_not_full_path(adapter, tmp_path):
    nested = tmp_path / "deep" / "deeper"
    nested.mkdir(parents=True)
    file = nested / "report.pdf"
    file.write_bytes(b"PDFDATA")
    chat_id = "a:dm"
    await adapter.send_document(chat_id, str(file))

    payload = next(iter(adapter._pending_uploads.values()))
    assert payload["filename"] == "report.pdf"
    # The card's content also carries the bare basename.
    activity = adapter._app.send.await_args.args[1]
    att = activity.attachments[0]
    assert att.name == "report.pdf"


# ---------------------------------------------------------------------------
# Channel (Graph SharePoint) dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_document_to_channel_uploads_via_graph(adapter, doc_file):
    chat_id = "19:abc@thread.tacv2"
    adapter._sharepoint_site_id = "site1"

    # Stub the lazy-built GraphClient so no MSAL / Graph SDK is touched.
    fake_graph = MagicMock()
    fake_graph.upload_to_sharepoint = AsyncMock(
        return_value="https://sp.example.com/x.pdf"
    )

    async def _ensure():
        adapter._graph = fake_graph
        return fake_graph

    adapter._ensure_graph = _ensure  # type: ignore[assignment]

    result = await adapter.send_document(chat_id, str(doc_file))

    assert result.success is True
    fake_graph.upload_to_sharepoint.assert_awaited_once()
    kwargs = fake_graph.upload_to_sharepoint.await_args.kwargs
    assert kwargs["site_id"] == "site1"
    assert kwargs["filename"] == "doc.pdf"
    assert kwargs["content"] == b"X" * 100
    # Folder is "<base>/<sanitized chat id>".
    assert kwargs["folder_path"] == "hermes/19_abc_at_thread.tacv2"
    # _app.send received an activity carrying a FileDownload attachment.
    adapter._app.send.assert_awaited_once()
    activity = adapter._app.send.await_args.args[1]
    assert activity.attachments[0].content_type == FILE_DOWNLOAD_INFO_CONTENT_TYPE


@pytest.mark.asyncio
async def test_send_document_to_channel_without_sharepoint_config_fails(
    adapter, doc_file
):
    adapter._sharepoint_site_id = ""
    result = await adapter.send_document("19:abc@thread.tacv2", str(doc_file))
    assert result.success is False
    assert result.retryable is False
    assert "SHAREPOINT" in (result.error or "").upper()


@pytest.mark.asyncio
async def test_send_document_to_channel_when_upload_fails(adapter, doc_file):
    adapter._sharepoint_site_id = "site1"
    fake_graph = MagicMock()
    fake_graph.upload_to_sharepoint = AsyncMock(return_value=None)

    async def _ensure():
        return fake_graph

    adapter._ensure_graph = _ensure  # type: ignore[assignment]
    result = await adapter.send_document("19:abc@thread.tacv2", str(doc_file))
    assert result.success is False
    assert result.retryable is True
    assert "upload failed" in (result.error or "").lower()


# ---------------------------------------------------------------------------
# Public send_video / send_voice — they just delegate.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_video_dispatches_through_send_local_file(adapter, doc_file):
    sentinel = SendResult(success=True, message_id="ok-video")
    adapter._send_local_file = AsyncMock(return_value=sentinel)  # type: ignore[assignment]
    result = await adapter.send_video("a:dm", str(doc_file), caption="demo")
    assert result is sentinel
    adapter._send_local_file.assert_awaited_once()
    args = adapter._send_local_file.await_args.args
    # (chat_id, path, caption, reply_to)
    assert args[0] == "a:dm"
    assert args[1] == str(doc_file)
    assert args[2] == "demo"


@pytest.mark.asyncio
async def test_send_voice_dispatches_through_send_local_file(adapter, doc_file):
    sentinel = SendResult(success=True, message_id="ok-voice")
    adapter._send_local_file = AsyncMock(return_value=sentinel)  # type: ignore[assignment]
    result = await adapter.send_voice("a:dm", str(doc_file))
    assert result is sentinel
    adapter._send_local_file.assert_awaited_once()


# ---------------------------------------------------------------------------
# _is_channel_chat heuristic
# ---------------------------------------------------------------------------


def test_is_channel_chat_conv_ref_says_channel(adapter):
    chat_id = "any-id-shape"
    ref = Mock()
    ref.conversation = Mock(conversation_type="channel")
    adapter._conv_refs[chat_id] = ref
    assert adapter._is_channel_chat(chat_id) is True


def test_is_channel_chat_conv_ref_says_personal_overrides_id_shape(adapter):
    # ID looks like a channel (19:...@thread.) but conv_ref says personal.
    chat_id = "19:foo@thread.bar"
    ref = Mock()
    ref.conversation = Mock(conversation_type="personal")
    adapter._conv_refs[chat_id] = ref
    assert adapter._is_channel_chat(chat_id) is False


def test_is_channel_chat_no_conv_ref_thread_substring_treated_as_channel(adapter):
    assert adapter._is_channel_chat("19:abc@thread.tacv2") is True


def test_is_channel_chat_no_conv_ref_dm_shape_returns_false(adapter):
    assert adapter._is_channel_chat("a:dm-thread-id") is False
    assert adapter._is_channel_chat("19:user@unq.gbl.spaces") is False


# ---------------------------------------------------------------------------
# _send_attachment dispatch — uses activity_sender when conv_ref present.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_attachment_uses_conv_ref_when_available(adapter, doc_file):
    chat_id = "a:dm-with-ref"
    ref = Mock()
    ref.conversation = Mock(conversation_type="personal")
    adapter._conv_refs[chat_id] = ref
    result = await adapter.send_document(chat_id, str(doc_file))
    assert result.success is True
    adapter._app.activity_sender.send.assert_awaited_once()
    adapter._app.send.assert_not_awaited()


# ---------------------------------------------------------------------------
# Constructor — extra config & env plumbing
# ---------------------------------------------------------------------------


def test_constructor_picks_up_sharepoint_config_from_extra(monkeypatch):
    for var in ("TEAMS_SHAREPOINT_SITE_ID", "TEAMS_SHAREPOINT_FOLDER"):
        monkeypatch.delenv(var, raising=False)
    cfg = PlatformConfig(
        enabled=True,
        extra={
            "client_id": "x",
            "client_secret": "y",
            "tenant_id": "z",
            "sharepoint_site_id": "site-from-config",
            "sharepoint_folder": "shared/hermes",
        },
    )
    a = TeamsAdapter(cfg)
    assert a._sharepoint_site_id == "site-from-config"
    assert a._sharepoint_folder == "shared/hermes"
    assert a._pending_uploads == {}
    assert a._graph is None
    assert a._token_provider is None


def test_constructor_falls_back_to_env_vars(monkeypatch):
    monkeypatch.setenv("TEAMS_SHAREPOINT_SITE_ID", "site-from-env")
    monkeypatch.setenv("TEAMS_SHAREPOINT_FOLDER", "env/folder")
    cfg = PlatformConfig(
        enabled=True,
        extra={"client_id": "x", "client_secret": "y", "tenant_id": "z"},
    )
    a = TeamsAdapter(cfg)
    assert a._sharepoint_site_id == "site-from-env"
    assert a._sharepoint_folder == "env/folder"


def test_constructor_default_folder_is_hermes(monkeypatch):
    for var in ("TEAMS_SHAREPOINT_SITE_ID", "TEAMS_SHAREPOINT_FOLDER"):
        monkeypatch.delenv(var, raising=False)
    cfg = PlatformConfig(
        enabled=True,
        extra={"client_id": "x", "client_secret": "y", "tenant_id": "z"},
    )
    a = TeamsAdapter(cfg)
    assert a._sharepoint_site_id == ""
    assert a._sharepoint_folder == "hermes"


# ---------------------------------------------------------------------------
# _pending_uploads memory bounds + send-failure cleanup (quality fixes)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_upload_dropped_when_send_fails(
    adapter, doc_file, monkeypatch, caplog
):
    """If FileConsent send fails, the stashed bytes must be popped."""
    monkeypatch.setattr(
        adapter,
        "_send_attachment",
        AsyncMock(
            return_value=SendResult(success=False, error="boom", retryable=True)
        ),
    )
    chat_id = "a:dm-thread-id"
    with caplog.at_level(logging.WARNING, logger="plugins.platforms.teams.adapter"):
        result = await adapter.send_document(chat_id, str(doc_file))

    assert result.success is False
    # Crucially: no leak of bytes when the user never got a card.
    assert len(adapter._pending_uploads) == 0
    assert any(
        "FileConsent send failed" in rec.getMessage()
        for rec in caplog.records
        if rec.levelno == logging.WARNING
    )


@pytest.mark.asyncio
async def test_pending_uploads_bounded_at_max(
    adapter, doc_file, monkeypatch, caplog
):
    """Beyond the cap, oldest entries are evicted FIFO with a warning."""
    monkeypatch.setattr(adapter, "_PENDING_UPLOAD_MAX", 3)

    captured_ids: list[str] = []

    # Send 4 documents, capturing the upload_id registered for each.
    with caplog.at_level(logging.WARNING, logger="plugins.platforms.teams.adapter"):
        for i in range(4):
            before = set(adapter._pending_uploads.keys())
            await adapter.send_document(f"a:dm-{i}", str(doc_file))
            after = set(adapter._pending_uploads.keys())
            new_ids = after - before
            # The send may both add and (on the 4th) evict; what we want
            # is the id that *got registered* this round.
            if new_ids:
                captured_ids.append(next(iter(new_ids)))
            else:
                # Registration + immediate eviction in same call shouldn't
                # happen at MAX=3 with one new send (cap holds 3, we add
                # the 4th, the oldest goes), so this branch is unexpected.
                captured_ids.append("<missing>")

    # Cap holds exactly MAX entries.
    assert len(adapter._pending_uploads) == 3
    # The first registered upload_id is gone.
    assert captured_ids[0] not in adapter._pending_uploads
    # The last 3 are present.
    for uid in captured_ids[1:]:
        assert uid in adapter._pending_uploads
    # The eviction emitted a warning.
    assert any(
        "evicted oldest" in rec.getMessage()
        for rec in caplog.records
        if rec.levelno == logging.WARNING
    )


@pytest.mark.asyncio
async def test_pending_uploads_evict_stale_by_ttl(adapter, doc_file):
    """Entries older than TTL are swept on the next DM send."""
    # First send: register a normal entry.
    await adapter.send_document("a:dm-old", str(doc_file))
    assert len(adapter._pending_uploads) == 1
    old_id = next(iter(adapter._pending_uploads))

    # Manually backdate the entry's timestamp past the TTL (2h ago).
    adapter._pending_uploads[old_id]["ts"] = time.monotonic() - 7200

    # Second send triggers _evict_stale_pending_uploads at the top of
    # _send_dm_file_consent, sweeping the stale entry before registering
    # the new one.
    await adapter.send_document("a:dm-new", str(doc_file))

    assert old_id not in adapter._pending_uploads
    # Only the fresh entry remains.
    assert len(adapter._pending_uploads) == 1


def test_register_pending_upload_sets_timestamp(adapter):
    """_register_pending_upload stamps a monotonic ts on the entry."""
    before = time.monotonic()
    adapter._register_pending_upload(
        "upload-xyz",
        {
            "filename": "f.pdf",
            "bytes": b"x",
            "chat_id": "a:dm",
            "caption": None,
            "reply_to": None,
        },
    )
    after = time.monotonic()

    entry = adapter._pending_uploads["upload-xyz"]
    assert "ts" in entry
    assert isinstance(entry["ts"], float)
    assert before <= entry["ts"] <= after
