# Follow-up: generalize loop-bridge for adapters with loop-bound SDK primitives

> **Status:** open
> **Filed:** 2026-05-14
> **Linked PR (the narrow fix this generalizes):** AmbulnzLLC/hermes-agent#2
> **Why this lives here:** issues are disabled on the AmbulnzLLC fork; tracking in-tree until the pilot moves to a tracker.

---

## Context

PR #2 fixed a cross-event-loop bug in Teams outbound sends with a **narrow** patch:

- `TeamsAdapter.connect()` captures `self._loop = asyncio.get_running_loop()`
- `tools/send_message_tool._send_teams` checks `getattr(adapter, "_loop", None)` and bridges via `asyncio.run_coroutine_threadsafe` when it differs from the caller's loop

That solves the immediate Teams problem but the bridge logic lives inside `_send_teams`. **Any future adapter with the same shape — SDK constructed on gateway loop, internal asyncio primitives bound to it — will trip the same trap and need its own copy of the bridge.**

Likely future tripwires:
- Webex (Bot Framework SDK)
- Zoom Apps SDK
- Google Chat (uses google-auth aiohttp client, internal lock)
- Possibly Mattermost if its socket client caches an Event

Yuanbao currently does **not** trip this (its send path is plain httpx/websocket I/O, loop-agnostic), but if its dispatcher ever pre-builds a Lock/Event we'd need the bridge there too.

## Proposal

Lift the bridge into the running-adapter registry layer. Two design options:

**Option A — Registry-level wrapper.** `tools/_running_adapters.py` returns adapters wrapped so that every coroutine method is auto-bridged when the call site's loop differs from `adapter._loop`.

Pros: callers never see the trap; one place to fix.
Cons: every adapter eats a thread hop on cross-loop, and async generators / streaming sends break unless the wrapper is shape-aware.

**Option B — Standardised mixin.** Add a `LoopBoundAdapter` mixin (`_loop` attribute + `_call_on_loop(coro)` helper). Adapters that need it inherit. `tools/send_message_tool` checks `isinstance(adapter, LoopBoundAdapter)` and calls `_call_on_loop`.

Pros: explicit, no surprise thread hops for adapters that don't need it.
Cons: adapter authors have to remember to inherit.

**Recommendation: B.** Loop-bound adapters are the minority and the mixin makes the contract visible at the class definition.

## Acceptance criteria

- [ ] `LoopBoundAdapter` (or equivalent) mixin in `gateway/platforms/base.py` with `_loop` capture in `connect()` and `_call_on_loop()` helper
- [ ] `TeamsAdapter` migrated to the mixin (drop the per-adapter `_loop` field)
- [ ] `tools/send_message_tool._send_teams` uses the mixin's bridge instead of its inline `_on_adapter_loop`
- [ ] Cross-loop test in `tests/tools/test_send_teams.py` still green
- [ ] Doc note in `docs/architecture/` describing the loop-binding pitfall and how the mixin solves it

## Out of scope

- Generalising to other adapters (Webex, Zoom, etc.) — those land when the adapters do.

## References

- Original RuntimeError: `<asyncio.locks.Event object at 0x... [unset]> is bound to a different event loop` from smoke test #1 of PR #2
- Narrow fix: commit `714aebcf6` on `feat/teams-outbound-files`
- Architecture writeup lives in `hermes-agent-pilot` skill ref `outbound-media-wiring-by-send-model.md` (private, AmbulnzLLC pilot repo)
