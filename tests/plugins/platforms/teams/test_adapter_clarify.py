"""Tests for clarify support and auth-helper refactor in the Teams adapter."""
from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from plugins.platforms.teams.adapter import TeamsAdapter
from plugins.platforms.teams import adapter as teams_adapter_module


@pytest.fixture(autouse=True)
def _bind_teams_sdk_symbols():
    """Bind the microsoft_teams SDK symbols before each test.

    The adapter no longer imports the SDK eagerly at module load — it detects
    presence via ``find_spec`` and binds the real symbols lazily in
    ``check_teams_requirements()`` (the #62935 .env-pollution fix). Card-rendering
    paths (``send_clarify``, ``_on_clarify_action``) dereference module globals
    like ``AdaptiveCard`` / ``TextBlock`` / ``Choice`` / ``ChoiceSetInput`` that
    stay ``None`` until that runs. In production ``connect()`` binds them first;
    here we call ``check_teams_requirements()`` to reproduce that state. Tests that
    assert on the unbound state (see ``TestLateImportBindings``) manage their
    own globals via monkeypatch, which pytest restores on teardown.
    """
    teams_adapter_module.check_teams_requirements()
    yield


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
        """Short-label path truncates the data payload to 200 chars defensively.

        Note: choices longer than LONG_CHOICE_THRESHOLD (30) trigger the
        ChoiceSet layout instead — covered separately. This test keeps every
        choice ≤ threshold so it stays on the per-button path.
        """
        adapter = _make_adapter()
        captured = {}
        async def fake_send_card(chat_id, card):
            captured["card"] = card
            return SimpleNamespace(id="msg-1")
        adapter._send_card = fake_send_card

        # 28 chars — under threshold so we stay on the short-label path
        sized_choice = "x" * 28
        await adapter.send_clarify(
            chat_id="c1",
            question="Q?",
            choices=[sized_choice],
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

    @pytest.mark.asyncio
    async def test_choice_path_arms_text_intercept_at_send_time(self):
        """Free-form text must resolve a choice clarify without clicking 'Other'.

        Regression: a user with a choice card up should be able to type
        free-form text in chat and have it captured as the clarify response.
        Pre-fix, the choice path only flipped ``awaiting_text`` when the user
        clicked the "Other" button, so typing in chat fell through to the
        agent loop instead of resolving the clarify.
        """
        adapter = _make_adapter()
        async def fake_send_card(chat_id, card):
            return SimpleNamespace(id="msg-armed")
        adapter._send_card = fake_send_card

        with patch("tools.clarify_gateway.mark_awaiting_text") as mark_mock:
            result = await adapter.send_clarify(
                chat_id="c1",
                question="Pick one or type something",
                choices=["A", "B"],
                clarify_id="cid_armed",
                session_key="s1",
            )

        assert result.success is True
        mark_mock.assert_called_once_with("cid_armed")

    @pytest.mark.asyncio
    async def test_short_choices_use_per_button_layout(self):
        """All choices ≤ 30 chars: render one ExecuteAction per choice (legacy layout)."""
        adapter = _make_adapter()
        captured = {}
        async def fake_send_card(chat_id, card):
            captured["card"] = card
            return SimpleNamespace(id="m")
        adapter._send_card = fake_send_card

        await adapter.send_clarify(
            chat_id="c1",
            question="Pick one",
            choices=["Apple", "Banana", "Cherry"],
            clarify_id="cid_short",
            session_key="s",
        )

        card = captured["card"]
        body = card.body if hasattr(card, "body") else card._body
        actions = card.actions if hasattr(card, "actions") else card._actions
        # No ChoiceSetInput in body
        body_types = [type(b).__name__ for b in body]
        assert "ChoiceSetInput" not in body_types
        # 3 choices + Other = 4 actions, each with hermes_clarify verb
        assert len(actions) == 4
        for i in range(3):
            data_i = _action_data(actions[i])
            assert data_i["choice_idx"] == i
            assert data_i["choice_text"] in ("Apple", "Banana", "Cherry")

    @pytest.mark.asyncio
    async def test_long_choice_triggers_choiceset_layout(self):
        """Any choice > 30 chars switches to Input.ChoiceSet so wrap honors the long label."""
        adapter = _make_adapter()
        captured = {}
        async def fake_send_card(chat_id, card):
            captured["card"] = card
            return SimpleNamespace(id="m")
        adapter._send_card = fake_send_card

        long_label = "Resume Step 3 (inbound attachment test) and then post the report"
        await adapter.send_clarify(
            chat_id="c1",
            question="How do you want to wrap up?",
            choices=["Short", long_label, "Other short"],
            clarify_id="cid_long",
            session_key="s",
        )

        card = captured["card"]
        body = card.body if hasattr(card, "body") else card._body
        actions = card.actions if hasattr(card, "actions") else card._actions

        # Body has a ChoiceSetInput with id "clarify_choice_value" carrying all 3 choices
        cs = next((b for b in body if type(b).__name__ == "ChoiceSetInput"), None)
        assert cs is not None, "Long-choice path should render Input.ChoiceSet in body"
        assert cs.id == "clarify_choice_value"
        cs_choices = cs.choices if hasattr(cs, "choices") else cs._choices
        assert len(cs_choices) == 3
        # Choice values are stringified indices, titles preserve full label
        assert [c.value for c in cs_choices] == ["0", "1", "2"]
        assert any(c.title == long_label for c in cs_choices), \
            "Long label must appear unclipped as a Choice title"

        # Actions: single Submit (kind=choiceset) + Other
        assert len(actions) == 2
        submit_data = _action_data(actions[0])
        assert submit_data["kind"] == "choiceset"
        assert submit_data["clarify_id"] == "cid_long"
        assert _action_data(actions[1])["choice_idx"] == "other"

    @pytest.mark.asyncio
    async def test_choiceset_threshold_boundary_uses_per_button_layout(self):
        """A choice exactly at the 30-char threshold must NOT trigger ChoiceSet."""
        adapter = _make_adapter()
        captured = {}
        async def fake_send_card(chat_id, card):
            captured["card"] = card
            return SimpleNamespace(id="m")
        adapter._send_card = fake_send_card

        boundary = "x" * 30  # exactly at threshold — stays on per-button path
        await adapter.send_clarify(
            chat_id="c1",
            question="Q?",
            choices=[boundary, "y"],
            clarify_id="cid_bound",
            session_key="s",
        )
        card = captured["card"]
        body = card.body if hasattr(card, "body") else card._body
        body_types = [type(b).__name__ for b in body]
        assert "ChoiceSetInput" not in body_types


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


class TestOnClarifyActionChoiceSet:
    """ChoiceSet layout: Submit click carries the input value; resolve must use it."""

    @pytest.mark.asyncio
    async def test_choiceset_submit_resolves_with_selected_choice(self, monkeypatch):
        """Submit on the ChoiceSet layout: data has kind=choiceset + clarify_choice_value."""
        monkeypatch.setenv("TEAMS_ALLOW_ALL_USERS", "true")
        adapter = _make_adapter()

        # Teams merges the ChoiceSetInput value (id "clarify_choice_value") into
        # the action's data dict at submit time.
        action = SimpleNamespace(
            verb="hermes_clarify",
            data={
                "clarify_id": "cid_cs",
                "session_key": "s",
                "kind": "choiceset",
                "clarify_choice_value": "1",  # user picked the second choice
            },
        )
        ctx = SimpleNamespace(
            activity=SimpleNamespace(
                value=SimpleNamespace(action=action),
                from_=SimpleNamespace(aad_object_id="u1", id="u1"),
            )
        )

        with patch("tools.clarify_gateway.resolve_gateway_clarify", return_value=True) as resolve_mock, \
             patch("tools.clarify_gateway._entries",
                   {"cid_cs": SimpleNamespace(question="Q?", choices=["First", "Second long label", "Third"])},
                   create=True):
            response = await adapter._on_clarify_action(ctx)

        # Should resolve with the second choice (idx 1) from entry.choices
        resolve_mock.assert_called_once_with("cid_cs", "Second long label")
        assert response.status == 200

    @pytest.mark.asyncio
    async def test_choiceset_submit_with_invalid_value_falls_back_to_zero(self, monkeypatch):
        """If clarify_choice_value is missing/garbage, default to first choice (idx 0)."""
        monkeypatch.setenv("TEAMS_ALLOW_ALL_USERS", "true")
        adapter = _make_adapter()

        action = SimpleNamespace(
            verb="hermes_clarify",
            data={
                "clarify_id": "cid_cs2",
                "session_key": "s",
                "kind": "choiceset",
                "clarify_choice_value": "garbage",
            },
        )
        ctx = SimpleNamespace(
            activity=SimpleNamespace(
                value=SimpleNamespace(action=action),
                from_=SimpleNamespace(aad_object_id="u1", id="u1"),
            )
        )

        with patch("tools.clarify_gateway.resolve_gateway_clarify", return_value=True) as resolve_mock, \
             patch("tools.clarify_gateway._entries",
                   {"cid_cs2": SimpleNamespace(question="Q?", choices=["A", "B"])},
                   create=True):
            await adapter._on_clarify_action(ctx)

        resolve_mock.assert_called_once_with("cid_cs2", "A")


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


class TestClarifyEdgeCases:
    @pytest.mark.asyncio
    async def test_missing_clarify_id_returns_unknown(self, monkeypatch):
        monkeypatch.setenv("TEAMS_ALLOW_ALL_USERS", "true")
        adapter = _make_adapter()
        action = SimpleNamespace(verb="hermes_clarify", data={"choice_idx": 0})
        ctx = SimpleNamespace(
            activity=SimpleNamespace(
                value=SimpleNamespace(action=action),
                from_=SimpleNamespace(aad_object_id="u1", id="u1"),
            )
        )
        response = await adapter._on_clarify_action(ctx)
        assert response.status == 200
        assert "Unknown" in (response.body.value or "")

    @pytest.mark.asyncio
    async def test_falls_back_to_entry_choices_when_text_missing(self, monkeypatch):
        """If choice_text is empty (corrupt payload), look up entry.choices[idx]."""
        monkeypatch.setenv("TEAMS_ALLOW_ALL_USERS", "true")
        adapter = _make_adapter()
        action = SimpleNamespace(
            verb="hermes_clarify",
            data={"clarify_id": "cid", "session_key": "s", "choice_idx": 1, "choice_text": ""},
        )
        ctx = SimpleNamespace(
            activity=SimpleNamespace(
                value=SimpleNamespace(action=action),
                from_=SimpleNamespace(aad_object_id="u1", id="u1"),
            )
        )
        with patch("tools.clarify_gateway._entries",
                   {"cid": SimpleNamespace(question="Q?", choices=["A", "B", "C"])},
                   create=True), \
             patch("tools.clarify_gateway.resolve_gateway_clarify", return_value=True) as resolve_mock:
            await adapter._on_clarify_action(ctx)
        resolve_mock.assert_called_once_with("cid", "B")


class TestLateImportBindings:
    """The Teams adapter has TWO import paths for microsoft_teams.cards symbols:

    1. Top-level ``try/except ImportError`` at module load time.
    2. ``check_teams_requirements()`` re-imports + re-binds the names when the
       lazy-deps installer pulls them in for deferred init.

    Whatever ``send_clarify`` references at runtime MUST be bound to the module
    namespace by BOTH paths. The deployed pod hit ``NameError: ChoiceSetInput
    is not defined`` because the late-init path only re-bound a subset of the
    symbols. This test guards against the same omission for any future symbol.
    """

    def test_late_init_binds_all_card_symbols_used_by_send_clarify(self):
        # If the deferred init succeeds in this test environment, all the
        # symbols send_clarify dereferences must be bound to module globals.
        result = teams_adapter_module.check_teams_requirements()
        if not result:
            pytest.skip("microsoft_teams SDK not available in this env")

        required = (
            "AdaptiveCard",
            "ExecuteAction",
            "TextBlock",
            "Choice",
            "ChoiceSetInput",
        )
        missing = [n for n in required if getattr(teams_adapter_module, n, None) is None]
        assert not missing, (
            f"check_teams_requirements() failed to bind: {missing}. "
            "Add the import to the deferred-init block AND the assignment line "
            "(see plugins/platforms/teams/adapter.py around lines 446 and 461)."
        )

    def test_check_teams_requirements_rebinds_globals_after_clear(self, monkeypatch):
        """Regression: ``check_teams_requirements()`` must rebind module globals via
        the ``global`` declaration, not just create local variables.

        The original deferred-init fix added ``Choice, ChoiceSetInput =
        _Choice, _ChoiceSetInput`` but forgot to declare them ``global``, so
        the assignments only set locals and the module namespace remained
        unchanged — reproducing the production NameError. This test simulates
        the deferred path by clearing the globals first, then calling
        ``check_teams_requirements()``, and asserts the names reappear.
        """
        # Skip if SDK isn't installed in this environment
        if not getattr(teams_adapter_module, "TEAMS_SDK_AVAILABLE", False):
            pytest.skip("microsoft_teams SDK not available in this env")

        symbols = ("AdaptiveCard", "ExecuteAction", "TextBlock", "Choice", "ChoiceSetInput")

        # Snapshot then clear via monkeypatch so pytest restores them on teardown
        for name in symbols + ("TEAMS_SDK_AVAILABLE", "AIOHTTP_AVAILABLE"):
            monkeypatch.setattr(teams_adapter_module, name, None, raising=False)

        # Sanity: globals are cleared
        for name in symbols:
            assert getattr(teams_adapter_module, name) is None, (
                f"setup failed: {name} not cleared"
            )

        # Now invoke the deferred-init path
        ok = teams_adapter_module.check_teams_requirements()
        assert ok, "check_teams_requirements() returned False after clearing globals"

        # All symbols must be re-bound to non-None values
        unbound = [n for n in symbols if getattr(teams_adapter_module, n, None) is None]
        assert not unbound, (
            f"check_teams_requirements() did not rebind module globals: {unbound}. "
            "This usually means the assignment is missing a ``global`` declaration "
            "at the top of the function — the assignment becomes a local instead "
            "of updating module state."
        )
