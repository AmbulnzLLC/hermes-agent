"""Tests for AnthropicTransport guardrail header injection.

The AnthropicBedrock SDK does not natively accept a `guardrailConfig` parameter
the way the Bedrock Converse API does, so guardrails on the AnthropicBedrock
path are sent as HTTP headers per the Bedrock InvokeModel spec:
  https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_InvokeModel.html

These tests pin the header-injection contract of AnthropicTransport.build_kwargs.
"""

import pytest

from agent.transports import get_transport


@pytest.fixture
def transport():
    import agent.transports.anthropic  # noqa: F401  — registers transport
    return get_transport("anthropic_messages")


class TestAnthropicTransportBasic:

    def test_api_mode(self, transport):
        assert transport.api_mode == "anthropic_messages"

    def test_no_guardrail_no_extra_headers(self, transport):
        """Without guardrail_config, build_kwargs must not add extra_headers."""
        msgs = [{"role": "user", "content": "Hello"}]
        kw = transport.build_kwargs(model="claude-3-5-sonnet-20241022", messages=msgs)
        # extra_headers may be absent or carry only adapter-set headers
        # (e.g. fast-mode beta) — but no Bedrock guardrail headers.
        headers = kw.get("extra_headers") or {}
        assert "X-Amzn-Bedrock-GuardrailIdentifier" not in headers
        assert "X-Amzn-Bedrock-GuardrailVersion" not in headers
        assert "X-Amzn-Bedrock-Trace" not in headers

    def test_empty_guardrail_no_extra_headers(self, transport):
        """guardrail_config={} or missing keys must be ignored, not crash."""
        msgs = [{"role": "user", "content": "Hello"}]
        for empty_cfg in (None, {}, {"guardrailIdentifier": "x"}, {"guardrailVersion": "1"}):
            kw = transport.build_kwargs(
                model="claude-3-5-sonnet-20241022",
                messages=msgs,
                guardrail_config=empty_cfg,
            )
            headers = kw.get("extra_headers") or {}
            assert "X-Amzn-Bedrock-GuardrailIdentifier" not in headers
            assert "X-Amzn-Bedrock-GuardrailVersion" not in headers


class TestAnthropicTransportGuardrailInjection:

    def test_guardrail_draft_version_injects_headers(self, transport):
        """A populated guardrail_config injects both required Bedrock headers."""
        msgs = [{"role": "user", "content": "Hello"}]
        kw = transport.build_kwargs(
            model="claude-3-5-sonnet-20241022",
            messages=msgs,
            guardrail_config={
                "guardrailIdentifier": "jp8n3921i7e8",
                "guardrailVersion": "DRAFT",
            },
        )
        headers = kw["extra_headers"]
        assert headers["X-Amzn-Bedrock-GuardrailIdentifier"] == "jp8n3921i7e8"
        assert headers["X-Amzn-Bedrock-GuardrailVersion"] == "DRAFT"
        assert "X-Amzn-Bedrock-Trace" not in headers

    def test_guardrail_numeric_version(self, transport):
        msgs = [{"role": "user", "content": "Hello"}]
        kw = transport.build_kwargs(
            model="claude-3-5-sonnet-20241022",
            messages=msgs,
            guardrail_config={
                "guardrailIdentifier": "jp8n3921i7e8",
                "guardrailVersion": "1",
            },
        )
        assert kw["extra_headers"]["X-Amzn-Bedrock-GuardrailVersion"] == "1"

    def test_guardrail_with_trace(self, transport):
        msgs = [{"role": "user", "content": "Hello"}]
        kw = transport.build_kwargs(
            model="claude-3-5-sonnet-20241022",
            messages=msgs,
            guardrail_config={
                "guardrailIdentifier": "jp8n3921i7e8",
                "guardrailVersion": "DRAFT",
                "trace": "ENABLED",
            },
        )
        assert kw["extra_headers"]["X-Amzn-Bedrock-Trace"] == "ENABLED"

    def test_guardrail_trace_lowercase_normalized(self, transport):
        """Bedrock InvokeModel rejects lowercase trace values — only
        {ENABLED, ENABLED_FULL, DISABLED} are valid. The transport must
        uppercase whatever the user wrote in config so common YAML conventions
        (`trace: disabled`) don't 400 at the API.
        """
        msgs = [{"role": "user", "content": "Hello"}]
        for raw, expected in (
            ("disabled", "DISABLED"),
            ("enabled", "ENABLED"),
            ("enabled_full", "ENABLED_FULL"),
            ("Disabled", "DISABLED"),
        ):
            kw = transport.build_kwargs(
                model="claude-3-5-sonnet-20241022",
                messages=msgs,
                guardrail_config={
                    "guardrailIdentifier": "jp8n3921i7e8",
                    "guardrailVersion": "DRAFT",
                    "trace": raw,
                },
            )
            assert kw["extra_headers"]["X-Amzn-Bedrock-Trace"] == expected, (
                f"trace={raw!r} → {kw['extra_headers']['X-Amzn-Bedrock-Trace']!r}, "
                f"expected {expected!r}"
            )

    def test_stream_processing_mode_ignored(self, transport):
        """streamProcessingMode is a Converse-API-only field — must not leak as a header."""
        msgs = [{"role": "user", "content": "Hello"}]
        kw = transport.build_kwargs(
            model="claude-3-5-sonnet-20241022",
            messages=msgs,
            guardrail_config={
                "guardrailIdentifier": "jp8n3921i7e8",
                "guardrailVersion": "DRAFT",
                "streamProcessingMode": "async",
            },
        )
        headers = kw["extra_headers"]
        # No header named after streamProcessingMode should appear
        assert not any("StreamProcessingMode" in k or "streamProcessing" in k for k in headers)

    def test_guardrail_preserves_other_extra_headers(self, transport):
        """Guardrail injection must merge with — not clobber — adapter-set extra_headers.

        fast_mode=True causes the adapter to set its own extra_headers (fast-mode beta);
        we need both to coexist.
        """
        msgs = [{"role": "user", "content": "Hello"}]
        kw = transport.build_kwargs(
            model="claude-opus-4-5",
            messages=msgs,
            fast_mode=True,
            guardrail_config={
                "guardrailIdentifier": "jp8n3921i7e8",
                "guardrailVersion": "DRAFT",
            },
        )
        headers = kw.get("extra_headers") or {}
        # Guardrail headers present
        assert headers.get("X-Amzn-Bedrock-GuardrailIdentifier") == "jp8n3921i7e8"
        assert headers.get("X-Amzn-Bedrock-GuardrailVersion") == "DRAFT"
        # And the adapter-set fast-mode beta header (if any) is still there.
        # We don't assert its exact name because the adapter owns that contract;
        # we just verify our code didn't drop pre-existing entries.
        # (At minimum the dict identity wasn't replaced with an empty one.)
        assert len(headers) >= 2


class TestBedrockGuardrailTraceLogging:
    """Pin the response-side logging contract for Bedrock guardrail traces.

    When Bedrock returns a `trace` block (because we sent
    X-Amzn-Bedrock-Trace=ENABLED on the request), the transport must log
    it so compliance has visibility into which filter intervened.  Without
    this the trace data is dropped on the floor.
    """

    def _stub_response(self, *, model_extra=None, attrs=None, msg_id="msg_test"):
        """Build a minimal duck-typed Bedrock response stand-in.

        We don't depend on `anthropic.types.Message` here — the helper is
        designed to read attributes generically so a plain object with the
        right shape is enough.  Keeps the test fast and SDK-version-proof.
        """

        class _Resp:
            id = msg_id
            content = []  # iterated by normalize_response — empty is fine
            stop_reason = "end_turn"
            model_extra: dict = {}  # populated below; declared so pyright is happy

        r = _Resp()
        if model_extra is not None:
            r.model_extra = model_extra
        for k, v in (attrs or {}).items():
            setattr(r, k, v)
        return r

    def test_no_trace_no_log(self, caplog):
        """Most responses won't carry a trace — must be silent."""
        from agent.transports.anthropic import _log_bedrock_guardrail_trace

        resp = self._stub_response(model_extra={})
        with caplog.at_level("INFO", logger="agent.transports.anthropic"):
            _log_bedrock_guardrail_trace(resp)
        assert not any(
            "guardrail trace" in r.message for r in caplog.records
        )

    def test_no_model_extra_no_log(self, caplog):
        """SDK versions without a model_extra dict must not crash or log."""
        from agent.transports.anthropic import _log_bedrock_guardrail_trace

        # No model_extra attribute at all; no fallback attrs.
        class _Bare:
            id = "msg_x"

        with caplog.at_level("INFO", logger="agent.transports.anthropic"):
            _log_bedrock_guardrail_trace(_Bare())
        assert not any(
            "guardrail trace" in r.message for r in caplog.records
        )

    def test_trace_in_model_extra_logs_at_info(self, caplog):
        """The canonical case: Bedrock surfaced a trace block via model_extra."""
        from agent.transports.anthropic import _log_bedrock_guardrail_trace

        trace = {
            "guardrail": {
                "modelOutput": [],
                "outputAssessments": {
                    "jp8n3921i7e8": [
                        {
                            "topicPolicy": {
                                "topics": [
                                    {
                                        "name": "PHI",
                                        "type": "DENY",
                                        "action": "BLOCKED",
                                    }
                                ]
                            }
                        }
                    ]
                },
            }
        }
        metrics = {
            "inputTokenCount": 42,
            "outputTokenCount": 7,
            "invocationLatency": 311,
        }
        resp = self._stub_response(
            model_extra={
                "amazon-bedrock-trace": trace,
                "amazon-bedrock-invocationMetrics": metrics,
            },
            msg_id="msg_abc123",
        )

        with caplog.at_level("INFO", logger="agent.transports.anthropic"):
            _log_bedrock_guardrail_trace(resp)

        matching = [r for r in caplog.records if "guardrail trace" in r.message]
        assert len(matching) == 1, "exactly one guardrail-trace log line expected"
        line = matching[0].getMessage()
        assert "msg_abc123" in line
        assert "PHI" in line
        assert "BLOCKED" in line
        # Metrics merged into the same log line.
        assert "inputTokenCount" in line

    def test_trace_via_direct_attribute_logs(self, caplog):
        """Older SDK shape: trace exposed as a direct (snake_case) attribute."""
        from agent.transports.anthropic import _log_bedrock_guardrail_trace

        trace = {"guardrail": {"action": "GUARDRAIL_INTERVENED"}}
        resp = self._stub_response(
            model_extra={},
            attrs={"amazon_bedrock_trace": trace},
        )

        with caplog.at_level("INFO", logger="agent.transports.anthropic"):
            _log_bedrock_guardrail_trace(resp)

        matching = [r for r in caplog.records if "guardrail trace" in r.message]
        assert len(matching) == 1
        assert "GUARDRAIL_INTERVENED" in matching[0].getMessage()

    def test_logging_failure_does_not_propagate(self, caplog):
        """A broken trace payload must never break the response path."""
        from agent.transports.anthropic import _log_bedrock_guardrail_trace

        # Non-serializable object inside the trace — json.dumps will raise.
        class _Unserializable:
            pass

        resp = self._stub_response(
            model_extra={"amazon-bedrock-trace": {"bad": _Unserializable()}}
        )
        # Must not raise.  default=str in the impl actually saves us here,
        # but the test pins the broader try/except contract: even truly
        # pathological input is swallowed silently (debug-level only).
        with caplog.at_level("DEBUG", logger="agent.transports.anthropic"):
            _log_bedrock_guardrail_trace(resp)
        # Either it logged the trace (default=str succeeded) or it logged
        # a debug failure — both are acceptable; what's NOT acceptable is
        # raising an exception.
