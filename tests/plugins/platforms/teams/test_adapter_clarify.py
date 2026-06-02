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


def _action_data(action) -> dict:
    """Return the action's data payload as a plain dict.

    The Teams SDK wraps action ``data`` in a Pydantic ``SubmitActionData``
    model that isn't subscriptable, so tests can't index it directly.
    """
    d = action.data
    if hasattr(d, "model_dump"):
        return d.model_dump()
    if isinstance(d, dict):
        return d
    return dict(d)


class TestSendClarifyChoices:
    """send_clarify with a non-empty choices list renders an Adaptive Card."""

    @pytest.mark.asyncio
    async def test_renders_card_with_button_per_choice_plus_other(self):
        adapter = _make_adapter()
        captured = {}
        async def fake_send_card(chat_id, card):
            captured["chat_id"] = chat_id
            captured["card"] = card
            return SimpleNamespace(id="msg-123")
        adapter._send_card = fake_send_card

        result = await adapter.send_clarify(
            chat_id="conv1",
            question="Which approach?",
            choices=["Option A", "Option B", "Option C"],
            clarify_id="cid01",
            session_key="sess1",
        )

        assert result.success is True
        assert result.message_id == "msg-123"
        card = captured["card"]
        # 3 choice buttons + 1 "Other" button = 4 actions
        actions = card.actions if hasattr(card, "actions") else card._actions
        assert len(actions) == 4
        # Each non-Other button has hermes_clarify verb
        for i in range(3):
            assert actions[i].verb == "hermes_clarify"
            data_i = _action_data(actions[i])
            assert data_i["clarify_id"] == "cid01"
            assert data_i["choice_idx"] == i
        # "Other" button is last, with sentinel index
        assert actions[3].verb == "hermes_clarify"
        assert _action_data(actions[3])["choice_idx"] == "other"

    @pytest.mark.asyncio
    async def test_truncates_long_choice_in_button_data(self):
        adapter = _make_adapter()
        captured = {}
        async def fake_send_card(chat_id, card):
            captured["card"] = card
            return SimpleNamespace(id="msg-1")
        adapter._send_card = fake_send_card

        long_choice = "x" * 500
        await adapter.send_clarify(
            chat_id="c1",
            question="Q?",
            choices=[long_choice],
            clarify_id="cid02",
            session_key="s",
        )

        card = captured["card"]
        actions = card.actions if hasattr(card, "actions") else card._actions
        # Truncated for payload — full text only in card body
        assert len(_action_data(actions[0])["choice_text"]) <= 200

    @pytest.mark.asyncio
    async def test_caps_choices_at_max(self):
        """clarify_tool itself caps at MAX_CHOICES=4, but the adapter must not crash if upstream sends more."""
        adapter = _make_adapter()
        captured = {}
        async def fake_send_card(chat_id, card):
            captured["card"] = card
            return SimpleNamespace(id="m")
        adapter._send_card = fake_send_card

        await adapter.send_clarify(
            chat_id="c1",
            question="Q?",
            choices=["A", "B", "C", "D", "E", "F"],  # 6 choices
            clarify_id="cid03",
            session_key="s",
        )
        card = captured["card"]
        actions = card.actions if hasattr(card, "actions") else card._actions
        # 4 choice buttons (capped) + 1 "Other" = 5 actions max
        assert len(actions) <= 5


class TestSendClarifyOpenEnded:
    """send_clarify with no choices uses plain text + mark_awaiting_text."""

    @pytest.mark.asyncio
    async def test_open_ended_calls_mark_awaiting_text_and_sends_plain(self):
        adapter = _make_adapter()
        sent = {}
        async def fake_send(chat_id, content, reply_to=None, metadata=None):
            sent["chat_id"] = chat_id
            sent["content"] = content
            from gateway.platforms.base import SendResult
            return SendResult(success=True, message_id="m1")
        adapter.send = fake_send

        with patch("tools.clarify_gateway.mark_awaiting_text") as mark_mock:
            result = await adapter.send_clarify(
                chat_id="c1",
                question="Free-form answer please?",
                choices=None,
                clarify_id="cid_open",
                session_key="s1",
            )
        assert result.success is True
        mark_mock.assert_called_once_with("cid_open")
        assert "Free-form answer please?" in sent["content"]

    @pytest.mark.asyncio
    async def test_empty_list_treated_as_open_ended(self):
        adapter = _make_adapter()
        async def fake_send(chat_id, content, reply_to=None, metadata=None):
            from gateway.platforms.base import SendResult
            return SendResult(success=True, message_id="m1")
        adapter.send = fake_send

        with patch("tools.clarify_gateway.mark_awaiting_text") as mark_mock:
            result = await adapter.send_clarify(
                chat_id="c1",
                question="Q?",
                choices=[],
                clarify_id="cid_empty",
                session_key="s1",
            )
        mark_mock.assert_called_once_with("cid_empty")


class TestOnClarifyActionChoice:
    """Clicking a choice button resolves the clarify and returns a confirmation card."""

    def _make_click_ctx(self, clarify_id: str, choice_idx, choice_text="Option A"):
        action = SimpleNamespace(
            verb="hermes_clarify",
            data={
                "clarify_id": clarify_id,
                "session_key": "s1",
                "choice_idx": choice_idx,
                "choice_text": choice_text,
            },
        )
        return SimpleNamespace(
            activity=SimpleNamespace(
                value=SimpleNamespace(action=action),
                from_=SimpleNamespace(aad_object_id="u1", id="u1"),
            )
        )

    @pytest.mark.asyncio
    async def test_choice_click_resolves_with_choice_text(self, monkeypatch):
        monkeypatch.setenv("TEAMS_ALLOW_ALL_USERS", "true")
        adapter = _make_adapter()

        ctx = self._make_click_ctx("cid01", 0, "Option A")

        with patch("tools.clarify_gateway.resolve_gateway_clarify", return_value=True) as resolve_mock, \
             patch("tools.clarify_gateway._entries", {"cid01": SimpleNamespace(question="Q?", choices=["Option A", "Option B"])}, create=True):
            response = await adapter._on_clarify_action(ctx)

        resolve_mock.assert_called_once_with("cid01", "Option A")
        assert response.status == 200

    @pytest.mark.asyncio
    async def test_unauthorized_click_does_not_resolve(self, monkeypatch):
        monkeypatch.delenv("TEAMS_ALLOWED_USERS", raising=False)
        monkeypatch.delenv("TEAMS_ALLOW_ALL_USERS", raising=False)
        adapter = _make_adapter()
        ctx = self._make_click_ctx("cid01", 0, "Option A")

        with patch("tools.clarify_gateway.resolve_gateway_clarify") as resolve_mock:
            response = await adapter._on_clarify_action(ctx)
        resolve_mock.assert_not_called()
        assert response.status == 200
        # Body carries the deny reason
        body_value = getattr(response.body, "value", "") or ""
        assert "TEAMS_ALLOWED_USERS" in body_value or "Not authorized" in body_value

    @pytest.mark.asyncio
    async def test_stale_card_returns_already_resolved_message(self, monkeypatch):
        """If the clarify entry is gone (resolved or timed out), return a friendly card update."""
        monkeypatch.setenv("TEAMS_ALLOW_ALL_USERS", "true")
        adapter = _make_adapter()
        ctx = self._make_click_ctx("cid_gone", 0, "Option A")

        with patch("tools.clarify_gateway._entries", {}, create=True), \
             patch("tools.clarify_gateway.resolve_gateway_clarify") as resolve_mock:
            response = await adapter._on_clarify_action(ctx)
        resolve_mock.assert_not_called()
        # Body should be a card with an "expired" message
        assert response.status == 200


class TestOnClarifyActionOther:
    """The 'Other' button must NOT resolve the clarify; it flips to text-capture."""

    @pytest.mark.asyncio
    async def test_other_click_calls_mark_awaiting_not_resolve(self, monkeypatch):
        monkeypatch.setenv("TEAMS_ALLOW_ALL_USERS", "true")
        adapter = _make_adapter()

        action = SimpleNamespace(
            verb="hermes_clarify",
            data={"clarify_id": "cid_other", "session_key": "s", "choice_idx": "other"},
        )
        ctx = SimpleNamespace(
            activity=SimpleNamespace(
                value=SimpleNamespace(action=action),
                from_=SimpleNamespace(aad_object_id="u1", id="u1"),
            )
        )

        with patch("tools.clarify_gateway._entries",
                   {"cid_other": SimpleNamespace(question="Q?", choices=["A", "B"])},
                   create=True), \
             patch("tools.clarify_gateway.resolve_gateway_clarify") as resolve_mock, \
             patch("tools.clarify_gateway.mark_awaiting_text") as mark_mock:
            response = await adapter._on_clarify_action(ctx)

        resolve_mock.assert_not_called()
        mark_mock.assert_called_once_with("cid_other")
        assert response.status == 200
