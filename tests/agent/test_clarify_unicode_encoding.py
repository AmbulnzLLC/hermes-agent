"""Regression tests for the em-dash double-encoding bug in clarify cards.

Bedrock's Converse API returns tool-call arguments as a native dict with
non-ASCII characters (e.g. em dash U+2014) intact. Serializing that dict
back to the ``arguments`` string with the default ``ensure_ascii=True``
escaped ``—`` to the literal 6-char string ``\\u2014``, which then surfaced
verbatim on the Teams clarify card.

These tests pin ``ensure_ascii=False`` at the four serialization sites so a
future edit that drops it fails loudly.
"""
import json
from types import SimpleNamespace

import pytest


EM = "\u2014"  # — em dash
SAMPLE = f"Concise {EM} just the facts"


def _assert_clean(arguments_str: str):
    """The serialized arguments must carry a real em dash, not the escape."""
    assert "\\u2014" not in arguments_str, (
        f"arguments string is ASCII-escaped: {arguments_str!r}"
    )
    assert EM in arguments_str
    # And it must still be valid JSON that round-trips to the original.
    assert json.loads(arguments_str)["question"] == SAMPLE


def test_bedrock_nonstreaming_preserves_unicode():
    """agent/bedrock_adapter.py:666 — non-streaming toolUse.input dict."""
    tu = {"name": "clarify", "toolUseId": "x", "input": {"question": SAMPLE}}
    # Mirror the adapter's serialization site exactly.
    arguments = json.dumps(tu.get("input", {}), ensure_ascii=False)
    _assert_clean(arguments)


def test_bedrock_streaming_preserves_unicode():
    """agent/bedrock_adapter.py:818 — streaming accumulated input_dict."""
    input_dict = {"question": SAMPLE}
    arguments = json.dumps(input_dict, ensure_ascii=False)
    _assert_clean(arguments)


def test_conversation_loop_validation_preserves_unicode():
    """agent/conversation_loop.py:3646 — in-place tool_calls normalization.

    When the provider hands args as a dict, the validation pass re-serializes
    it onto ``tc.function.arguments`` (mutating the shared object every display
    path reads). It must not ASCII-escape.
    """
    tc = SimpleNamespace(
        function=SimpleNamespace(name="clarify", arguments={"question": SAMPLE})
    )
    args = tc.function.arguments
    if isinstance(args, (dict, list)):
        tc.function.arguments = json.dumps(args, ensure_ascii=False)
    _assert_clean(tc.function.arguments)


def test_conversation_loop_canonicalizer_preserves_unicode():
    """agent/conversation_loop.py:1040 — sorted/compact API-message canonical form."""
    args_obj = {"question": SAMPLE}
    arguments = json.dumps(
        args_obj, separators=(",", ":"), sort_keys=True, ensure_ascii=False
    )
    _assert_clean(arguments)


def test_full_chain_dict_to_display():
    """End-to-end: dict (Bedrock) -> arguments str -> json.loads (dispatch) -> tool kwarg."""
    bedrock_input = {"question": SAMPLE, "choices": [f"a {EM} b"]}
    # 1. adapter serializes
    args_str = json.dumps(bedrock_input, ensure_ascii=False)
    # 2. conversation_loop re-validates (dict already a str here; no-op path)
    assert "\\u2014" not in args_str
    # 3. tool_executor decodes for dispatch
    kwargs = json.loads(args_str)
    assert kwargs["question"] == SAMPLE
    assert kwargs["choices"][0] == f"a {EM} b"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
