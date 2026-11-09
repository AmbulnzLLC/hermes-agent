"""send_message -> Teams: text and MEDIA route through the live gateway adapter.

Teams has no standalone sender in this fork: FileConsent (DMs) and SharePoint
(channels) uploads need the running ``TeamsAdapter``. ``_send_to_platform``
must therefore hand media to the live adapter (``_ADAPTER_NATIVE_MEDIA``)
instead of dropping it with the generic "media not supported" warning, and
``_send_live_adapter_media`` picks the adapter method by file extension.

The adapter is a real ``TeamsAdapter`` built with ``__new__`` (no Bot
Framework auth, no ConversationReference): the router checks that the
*class* overrides each ``BasePlatformAdapter`` media stub, so a bare mock
would not pin the contract. Only the instance's send methods are mocked.
"""

import asyncio
import sys
import threading
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform

pytest.importorskip("microsoft_teams.apps")

from plugins.platforms.teams.adapter import TeamsAdapter  # noqa: E402
from tools.send_message_tool import _send_to_platform  # noqa: E402

TEAMS = Platform("teams")
PCONFIG = SimpleNamespace(extra={})


def _result(success=True, message_id="msg-1", error=None):
    return SimpleNamespace(success=success, message_id=message_id, error=error,
                           retryable=False, raw_response=None)


def _teams_adapter():
    adapter = TeamsAdapter.__new__(TeamsAdapter)
    adapter.send = AsyncMock(return_value=_result(message_id="text-1"))
    adapter.send_image_file = AsyncMock(return_value=_result(message_id="img-1"))
    adapter.send_video = AsyncMock(return_value=_result(message_id="vid-1"))
    adapter.send_voice = AsyncMock(return_value=_result(message_id="voice-1"))
    adapter.send_document = AsyncMock(return_value=_result(message_id="doc-1"))
    return adapter


def _media_mocks(adapter):
    return [adapter.send_image_file, adapter.send_video, adapter.send_voice, adapter.send_document]


@pytest.fixture
def install_runner(monkeypatch):
    """Expose ``adapter`` as the in-process gateway's live Teams adapter."""
    def _install(adapter, gateway_loop=None):
        runner = SimpleNamespace(adapters={TEAMS: adapter} if adapter else {},
                                 _gateway_loop=gateway_loop)
        fake = ModuleType("gateway.run")
        fake._gateway_runner_ref = lambda: runner
        monkeypatch.setitem(sys.modules, "gateway.run", fake)
        return runner
    return _install


@pytest.fixture
def media(tmp_path):
    def _make(name, data=b"x"):
        path = tmp_path / name
        path.write_bytes(data)
        return str(path)
    return _make


def _send(message, media_files=None, **kw):
    return asyncio.run(_send_to_platform(TEAMS, PCONFIG, "a:abc", message, media_files=media_files, **kw))


@pytest.mark.parametrize("name,is_voice,method", [
    ("report.pdf", False, "send_document"),
    ("shot.png", False, "send_image_file"),
    ("clip.mp4", False, "send_video"),
    ("voice.ogg", True, "send_voice"),
])
def test_media_routes_to_adapter_method_by_extension(install_runner, media, name, is_voice, method):
    adapter = _teams_adapter()
    install_runner(adapter)

    result = _send("", [(media(name), is_voice)])

    assert result.get("success") is True, result
    assert result.get("media_delivered") is True
    assert "warnings" not in result  # not dropped by the generic text-only path
    getattr(adapter, method).assert_awaited_once()
    for other in _media_mocks(adapter):
        if other is not getattr(adapter, method):
            other.assert_not_awaited()


def test_force_document_sends_image_as_document(install_runner, media):
    adapter = _teams_adapter()
    install_runner(adapter)

    result = _send("", [(media("shot.png"), False)], force_document=True)

    assert result.get("success") is True, result
    adapter.send_document.assert_awaited_once()
    adapter.send_image_file.assert_not_awaited()


def test_short_text_rides_as_caption_on_the_file(install_runner, media):
    adapter = _teams_adapter()
    install_runner(adapter)

    result = _send("here is the report", [(media("report.pdf"), False)])

    assert result.get("success") is True, result
    adapter.send.assert_not_awaited()
    assert adapter.send_document.await_args.kwargs["caption"] == "here is the report"


def test_text_only_uses_send_and_no_media_methods(install_runner):
    adapter = _teams_adapter()
    install_runner(adapter)

    result = _send("just text")

    assert result == {"success": True, "message_id": "text-1"}
    adapter.send.assert_awaited_once()
    for m in _media_mocks(adapter):
        m.assert_not_awaited()


def test_no_live_adapter_falls_back_to_the_standalone_sender(install_runner, monkeypatch):
    """Out of process (cron) the plugin's Bot Framework REST sender takes over; with no
    credentials it fails with its own error rather than crashing."""
    for var in ("TEAMS_CLIENT_ID", "TEAMS_CLIENT_SECRET", "TEAMS_TENANT_ID"):
        monkeypatch.delenv(var, raising=False)
    install_runner(None)

    result = _send("hi")

    assert "Teams standalone send" in result.get("error", ""), result


def test_missing_media_file_is_reported(install_runner):
    adapter = _teams_adapter()
    install_runner(adapter)

    result = _send("", [("/nonexistent/path.pdf", False)])

    assert "not found" in result.get("error", ""), result
    adapter.send_document.assert_not_awaited()


def test_adapter_failure_is_surfaced(install_runner, media):
    adapter = _teams_adapter()
    adapter.send_document = AsyncMock(return_value=_result(success=False, error="upload denied"))
    install_runner(adapter)

    result = _send("", [(media("report.pdf"), False)])

    assert "upload denied" in result.get("error", ""), result


def test_media_send_runs_on_the_gateway_loop_when_called_from_another(install_runner, media):
    """The Teams SDK's App holds asyncio primitives bound to the gateway loop; a tool
    worker loop must hop there rather than await the upload inline."""
    gateway_loop = asyncio.new_event_loop()
    ready = threading.Event()

    def _run_loop():
        asyncio.set_event_loop(gateway_loop)
        ready.set()
        gateway_loop.run_forever()

    thread = threading.Thread(target=_run_loop, daemon=True)
    thread.start()
    ready.wait(timeout=2)
    try:
        ran_on = {}

        async def _send_document(*args, **kwargs):
            ran_on["loop"] = asyncio.get_running_loop()
            return _result(message_id="doc-x")

        adapter = _teams_adapter()
        adapter.send_document = AsyncMock(side_effect=_send_document)
        install_runner(adapter, gateway_loop=gateway_loop)

        result = _send("", [(media("report.pdf"), False)])

        assert result.get("success") is True, result
        assert ran_on["loop"] is gateway_loop
    finally:
        gateway_loop.call_soon_threadsafe(gateway_loop.stop)
        thread.join(timeout=2)
        gateway_loop.close()
