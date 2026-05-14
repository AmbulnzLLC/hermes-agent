"""Tests for ``_send_teams`` — outbound media path through the running
Teams adapter.

Companion to ``tests/tools/test_running_adapters.py``. The registry is
the plumbing; ``_send_teams`` is the actual outbound call site that
``send_message_tool.py`` dispatches to when a caller sends to Teams
with ``MEDIA:/path`` payloads.

Why these tests use a mock adapter rather than a real ``TeamsAdapter``:
the real adapter requires Microsoft Bot Framework auth, an Azure
service URL, and an established ``ConversationReference`` — none of
which fits a unit test. The tests verify ``_send_teams`` correctly
*dispatches* to the registered adapter's ``send_*`` methods by file
extension, which is the contract the helper owes its caller.
"""

import asyncio
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


def _run(coro):
    return asyncio.run(coro)


def _make_send_result(success=True, message_id="msg-1", error=None):
    """SendResult-shaped object — duck-typed so we don't import the
    base class (which would drag the full plugin-registry loader)."""
    return SimpleNamespace(
        success=success,
        message_id=message_id,
        error=error,
        retryable=False,
    )


def _register_mock_adapter():
    """Register a fresh mock adapter for ``teams`` and return it."""
    from tools._running_adapters import (
        clear_running_adapters,
        set_running_adapter,
    )

    clear_running_adapters()
    adapter = MagicMock()
    adapter.send = AsyncMock(return_value=_make_send_result(message_id="text-1"))
    adapter.send_image_file = AsyncMock(return_value=_make_send_result(message_id="img-1"))
    adapter.send_video = AsyncMock(return_value=_make_send_result(message_id="vid-1"))
    adapter.send_voice = AsyncMock(return_value=_make_send_result(message_id="voice-1"))
    adapter.send_document = AsyncMock(return_value=_make_send_result(message_id="doc-1"))
    set_running_adapter("teams", adapter)
    return adapter


@pytest.fixture
def tmp_pdf(tmp_path):
    """A throwaway file with a non-image extension — exercises the
    document branch."""
    path = tmp_path / "report.pdf"
    path.write_bytes(b"%PDF-1.4\n")
    return str(path)


@pytest.fixture
def tmp_png(tmp_path):
    path = tmp_path / "shot.png"
    path.write_bytes(b"\x89PNG\r\n")
    return str(path)


@pytest.fixture
def tmp_mp4(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftyp")
    return str(path)


@pytest.fixture
def tmp_ogg(tmp_path):
    path = tmp_path / "voice.ogg"
    path.write_bytes(b"OggS")
    return str(path)


def test_send_teams_routes_pdf_to_send_document(tmp_pdf):
    """A non-image, non-video, non-voice path lands on send_document."""
    from tools.send_message_tool import _send_teams

    adapter = _register_mock_adapter()
    result = _run(
        _send_teams(
            chat_id="a:abc",
            message="here is the report",
            media_files=[(tmp_pdf, False)],
        )
    )

    assert result.get("success") is True
    assert result.get("platform") == "teams"
    adapter.send.assert_awaited_once()
    adapter.send_document.assert_awaited_once()
    adapter.send_image_file.assert_not_awaited()
    adapter.send_video.assert_not_awaited()
    adapter.send_voice.assert_not_awaited()


def test_send_teams_routes_png_to_send_image_file(tmp_png):
    from tools.send_message_tool import _send_teams

    adapter = _register_mock_adapter()
    result = _run(
        _send_teams(
            chat_id="a:abc",
            message="",
            media_files=[(tmp_png, False)],
        )
    )

    assert result.get("success") is True
    adapter.send_image_file.assert_awaited_once()
    adapter.send_document.assert_not_awaited()


def test_send_teams_routes_mp4_to_send_video(tmp_mp4):
    from tools.send_message_tool import _send_teams

    adapter = _register_mock_adapter()
    result = _run(
        _send_teams(
            chat_id="a:abc",
            message="",
            media_files=[(tmp_mp4, False)],
        )
    )

    assert result.get("success") is True
    adapter.send_video.assert_awaited_once()


def test_send_teams_routes_ogg_voice_to_send_voice(tmp_ogg):
    """``is_voice=True`` for an ogg/opus file picks the voice path."""
    from tools.send_message_tool import _send_teams

    adapter = _register_mock_adapter()
    result = _run(
        _send_teams(
            chat_id="a:abc",
            message="",
            media_files=[(tmp_ogg, True)],
        )
    )

    assert result.get("success") is True
    adapter.send_voice.assert_awaited_once()


def test_send_teams_returns_error_when_no_running_adapter(tmp_pdf):
    """If the gateway hasn't connected the Teams adapter, the helper
    returns a clear error rather than crashing or silently dropping."""
    from tools._running_adapters import clear_running_adapters
    from tools.send_message_tool import _send_teams

    clear_running_adapters()
    result = _run(
        _send_teams(
            chat_id="a:abc",
            message="hi",
            media_files=[(tmp_pdf, False)],
        )
    )

    assert "error" in result
    assert "teams" in result["error"].lower()
    assert "not connected" in result["error"].lower() or "running adapter" in result["error"].lower()


def test_send_teams_returns_error_when_media_file_missing():
    """Bad media path is reported, not silently swallowed."""
    from tools.send_message_tool import _send_teams

    _register_mock_adapter()
    result = _run(
        _send_teams(
            chat_id="a:abc",
            message="",
            media_files=[("/nonexistent/path.pdf", False)],
        )
    )

    assert "error" in result
    assert "not found" in result["error"].lower()


def test_send_teams_propagates_adapter_failure(tmp_pdf):
    """If the live adapter returns ``success=False``, surface the error."""
    from tools._running_adapters import (
        clear_running_adapters,
        set_running_adapter,
    )
    from tools.send_message_tool import _send_teams

    clear_running_adapters()
    adapter = MagicMock()
    adapter.send = AsyncMock(return_value=_make_send_result(success=True))
    adapter.send_document = AsyncMock(
        return_value=_make_send_result(success=False, error="upload denied")
    )
    set_running_adapter("teams", adapter)

    result = _run(
        _send_teams(
            chat_id="a:abc",
            message="",
            media_files=[(tmp_pdf, False)],
        )
    )

    assert "error" in result
    assert "upload denied" in result["error"]


def test_send_teams_text_only_does_not_touch_media_methods():
    """A text-only call (no MEDIA tag) just hits ``send`` once."""
    from tools.send_message_tool import _send_teams

    adapter = _register_mock_adapter()
    result = _run(
        _send_teams(
            chat_id="a:abc",
            message="just text",
            media_files=[],
        )
    )

    assert result.get("success") is True
    adapter.send.assert_awaited_once()
    adapter.send_document.assert_not_awaited()
    adapter.send_image_file.assert_not_awaited()
    adapter.send_video.assert_not_awaited()
    adapter.send_voice.assert_not_awaited()
