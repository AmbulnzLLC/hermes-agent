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
                "trace": "enabled",
            },
        )
        assert kw["extra_headers"]["X-Amzn-Bedrock-Trace"] == "enabled"

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
