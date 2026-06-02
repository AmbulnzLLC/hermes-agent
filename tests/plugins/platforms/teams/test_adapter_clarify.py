"""Tests for clarify support and auth-helper refactor in the Teams adapter."""
from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from plugins.platforms.teams.adapter import TeamsAdapter


def _make_adapter() -> TeamsAdapter:
    """Build an adapter with the minimum scaffolding tests need."""
    a = TeamsAdapter.__new__(TeamsAdapter)
    a._app = MagicMock()
    a._conv_refs = {}
    # Note: TeamsAdapter.name is a read-only property, so we don't set it here.
    # The helper under test doesn't read self.name anyway.
    return a


def _ctx_with_clicker(aad_oid: str | None = None, fallback_id: str = ""):
    """Build a fake ActivityContext with the from_ field shape."""
    from_account = SimpleNamespace(aad_object_id=aad_oid, id=fallback_id)
    return SimpleNamespace(activity=SimpleNamespace(from_=from_account))


class TestAuthorizeCardClicker:
    """Pure refactor: behavior must match the inlined logic in _on_card_action."""

    def test_default_deny_when_no_env_set(self, monkeypatch):
        monkeypatch.delenv("TEAMS_ALLOWED_USERS", raising=False)
        monkeypatch.delenv("TEAMS_ALLOW_ALL_USERS", raising=False)
        adapter = _make_adapter()
        ctx = _ctx_with_clicker(aad_oid="user-1")
        ok, reason = adapter._authorize_card_clicker(ctx)
        assert ok is False
        assert "TEAMS_ALLOWED_USERS" in reason

    def test_allow_all_bypasses_allowlist(self, monkeypatch):
        monkeypatch.setenv("TEAMS_ALLOW_ALL_USERS", "true")
        monkeypatch.delenv("TEAMS_ALLOWED_USERS", raising=False)
        adapter = _make_adapter()
        ctx = _ctx_with_clicker(aad_oid="user-1")
        ok, _ = adapter._authorize_card_clicker(ctx)
        assert ok is True

    def test_allowlist_match(self, monkeypatch):
        monkeypatch.setenv("TEAMS_ALLOWED_USERS", "user-1,user-2")
        monkeypatch.delenv("TEAMS_ALLOW_ALL_USERS", raising=False)
        adapter = _make_adapter()
        ctx = _ctx_with_clicker(aad_oid="user-2")
        ok, _ = adapter._authorize_card_clicker(ctx)
        assert ok is True

    def test_allowlist_miss(self, monkeypatch):
        monkeypatch.setenv("TEAMS_ALLOWED_USERS", "user-1")
        monkeypatch.delenv("TEAMS_ALLOW_ALL_USERS", raising=False)
        adapter = _make_adapter()
        ctx = _ctx_with_clicker(aad_oid="user-99")
        ok, reason = adapter._authorize_card_clicker(ctx)
        assert ok is False
        assert "Not authorized" in reason

    def test_wildcard_in_allowlist(self, monkeypatch):
        monkeypatch.setenv("TEAMS_ALLOWED_USERS", "*")
        monkeypatch.delenv("TEAMS_ALLOW_ALL_USERS", raising=False)
        adapter = _make_adapter()
        ctx = _ctx_with_clicker(aad_oid="anyone")
        ok, _ = adapter._authorize_card_clicker(ctx)
        assert ok is True


class TestVerbRouting:
    """The card action dispatcher must route hermes_clarify to _on_clarify_action."""

    @pytest.mark.asyncio
    async def test_hermes_clarify_verb_dispatches_to_clarify_handler(self, monkeypatch):
        monkeypatch.setenv("TEAMS_ALLOW_ALL_USERS", "true")
        adapter = _make_adapter()

        # Build a card-action context with verb=hermes_clarify
        action = SimpleNamespace(verb="hermes_clarify", data={"clarify_id": "abc123", "choice_idx": 0})
        ctx = SimpleNamespace(
            activity=SimpleNamespace(
                value=SimpleNamespace(action=action),
                from_=SimpleNamespace(aad_object_id="u1", id="u1"),
            )
        )

        # Patch _on_clarify_action to a sentinel
        sentinel = MagicMock(return_value="CLARIFY_HANDLED")
        async def fake_clarify_handler(c):
            return sentinel(c)
        adapter._on_clarify_action = fake_clarify_handler

        result = await adapter._on_card_action(ctx)
        sentinel.assert_called_once_with(ctx)
        assert result == "CLARIFY_HANDLED"

    @pytest.mark.asyncio
    async def test_unknown_verb_falls_through_to_approval_path(self, monkeypatch):
        """Defensive: a missing/unknown verb should not be misrouted to clarify."""
        monkeypatch.setenv("TEAMS_ALLOW_ALL_USERS", "true")
        adapter = _make_adapter()
        # Action with verb=hermes_approve and a known approval shape — should hit approval logic,
        # not clarify. We assert clarify handler is NOT called.
        action = SimpleNamespace(
            verb="hermes_approve",
            data={"hermes_action": "approve_once", "session_key": "sess1", "cmd": "ls", "desc": "test"},
        )
        ctx = SimpleNamespace(
            activity=SimpleNamespace(
                value=SimpleNamespace(action=action),
                from_=SimpleNamespace(aad_object_id="u1", id="u1"),
            )
        )
        clarify_mock = MagicMock()
        async def fake_clarify_handler(c):
            clarify_mock(c)
        adapter._on_clarify_action = fake_clarify_handler

        with patch("tools.approval.has_blocking_approval", return_value=False):
            await adapter._on_card_action(ctx)
        clarify_mock.assert_not_called()
