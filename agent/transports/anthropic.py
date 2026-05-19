"""Anthropic Messages API transport.

Delegates to the existing adapter functions in agent/anthropic_adapter.py.
This transport owns format conversion and normalization — NOT client lifecycle.
"""

import logging
from typing import Any, Dict, List, Optional

from agent.transports.base import ProviderTransport
from agent.transports.types import NormalizedResponse

logger = logging.getLogger(__name__)


def _log_bedrock_guardrail_trace(response: Any) -> None:
    """Emit a log line when Bedrock returned a guardrail trace.

    Bedrock attaches the trace as `amazon-bedrock-trace` (and supporting
    `amazon-bedrock-invocationMetrics`) to the InvokeModel response body.
    The AnthropicBedrock SDK drops these into pydantic's `model_extra`
    catch-all because the typed Anthropic Message schema doesn't declare
    them.  We pull from there, with `getattr` fallbacks for SDK versions
    that surface them as direct attributes.

    No-op when the trace block is absent (the request didn't ask for it,
    or `ENABLED` mode and the guardrail didn't intervene).  Best-effort —
    swallows exceptions so a logging hiccup never breaks a real response.
    """
    try:
        extras: Dict[str, Any] = {}
        model_extra = getattr(response, "model_extra", None)
        if isinstance(model_extra, dict):
            extras = model_extra

        # Two known-on-the-wire keys.  `amazon-bedrock-trace` carries the
        # guardrail assessment block; `amazon-bedrock-invocationMetrics`
        # carries token + latency counts.  Both can also live as direct
        # attributes depending on SDK version, so try both spellings.
        trace = extras.get("amazon-bedrock-trace") or getattr(
            response, "amazon_bedrock_trace", None
        )
        metrics = extras.get("amazon-bedrock-invocationMetrics") or getattr(
            response, "amazon_bedrock_invocation_metrics", None
        )

        # The trace dict's `guardrail` key is what compliance cares about:
        # it lists which filter (topic, content, sensitive_info, contextual
        # grounding, word) intervened, the action taken, and the per-filter
        # confidence.  Log the whole thing — JSON-serialized for grep-ability
        # and to keep the structured payload intact for downstream parsing.
        import json

        payload: Dict[str, Any] = {"trace": trace}
        if metrics:
            payload["metrics"] = metrics
        msg_id = getattr(response, "id", None)
        if msg_id:
            payload["message_id"] = msg_id

        logger.info(
            "bedrock guardrail trace: %s",
            json.dumps(payload, default=str, sort_keys=True),
        )
    except Exception as e:  # noqa: BLE001 — never let logging break the response
        logger.debug("failed to surface bedrock guardrail trace: %s", e)


class AnthropicTransport(ProviderTransport):
    """Transport for api_mode='anthropic_messages'.

    Wraps the existing functions in anthropic_adapter.py behind the
    ProviderTransport ABC.  Each method delegates — no logic is duplicated.
    """

    @property
    def api_mode(self) -> str:
        return "anthropic_messages"

    def convert_messages(self, messages: List[Dict[str, Any]], **kwargs) -> Any:
        """Convert OpenAI messages to Anthropic (system, messages) tuple.

        kwargs:
            base_url: Optional[str] — affects thinking signature handling.
        """
        from agent.anthropic_adapter import convert_messages_to_anthropic

        base_url = kwargs.get("base_url")
        return convert_messages_to_anthropic(messages, base_url=base_url)

    def convert_tools(self, tools: List[Dict[str, Any]]) -> Any:
        """Convert OpenAI tool schemas to Anthropic input_schema format."""
        from agent.anthropic_adapter import convert_tools_to_anthropic

        return convert_tools_to_anthropic(tools)

    def build_kwargs(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        **params,
    ) -> Dict[str, Any]:
        """Build Anthropic messages.create() kwargs.

        Calls convert_messages and convert_tools internally.

        params (all optional):
            max_tokens: int
            reasoning_config: dict | None
            tool_choice: str | None
            is_oauth: bool
            preserve_dots: bool
            context_length: int | None
            base_url: str | None
            fast_mode: bool
            drop_context_1m_beta: bool
            guardrail_config: dict | None — Bedrock guardrails. When set,
                injects X-Amzn-Bedrock-Guardrail{Identifier,Version,Trace}
                headers via extra_headers on the AnthropicBedrock SDK call.
                Keys consumed: guardrailIdentifier, guardrailVersion, trace.
                (streamProcessingMode is Converse-API-only and ignored here.)
                Caller is responsible for only setting this on Bedrock; the
                transport itself does not gate by provider.
        """
        from agent.anthropic_adapter import build_anthropic_kwargs

        kwargs = build_anthropic_kwargs(
            model=model,
            messages=messages,
            tools=tools,
            max_tokens=params.get("max_tokens", 16384),
            reasoning_config=params.get("reasoning_config"),
            tool_choice=params.get("tool_choice"),
            is_oauth=params.get("is_oauth", False),
            preserve_dots=params.get("preserve_dots", False),
            context_length=params.get("context_length"),
            base_url=params.get("base_url"),
            fast_mode=params.get("fast_mode", False),
            drop_context_1m_beta=params.get("drop_context_1m_beta", False),
        )

        # Bedrock guardrail header injection.
        # The AnthropicBedrock SDK doesn't expose a `guardrailConfig` parameter
        # (that's a Converse-API field), so guardrails on the AnthropicBedrock
        # path must be sent as HTTP headers per the Bedrock InvokeModel spec:
        # https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_InvokeModel.html
        # These headers are also what the IAM `bedrock:GuardrailIdentifier`
        # condition key checks against, so they're load-bearing for IAM-enforced
        # guardrail policies — without them, IAM Deny statements on the policy
        # cause every Claude-on-Bedrock InvokeModel call to 403.
        guardrail = params.get("guardrail_config")
        if guardrail and guardrail.get("guardrailIdentifier") and guardrail.get("guardrailVersion"):
            extra_headers = dict(kwargs.get("extra_headers") or {})
            extra_headers["X-Amzn-Bedrock-GuardrailIdentifier"] = guardrail["guardrailIdentifier"]
            extra_headers["X-Amzn-Bedrock-GuardrailVersion"] = guardrail["guardrailVersion"]
            # Bedrock's InvokeModel API rejects lowercase trace values — the
            # accepted set is {ENABLED, ENABLED_FULL, DISABLED}. Normalize so
            # configs written as `trace: disabled` (or `enabled`) don't 400.
            _trace = guardrail.get("trace")
            if _trace:
                extra_headers["X-Amzn-Bedrock-Trace"] = str(_trace).upper()
            kwargs["extra_headers"] = extra_headers
            logger.info(
                "bedrock guardrail headers attached: %s",
                sorted(extra_headers.keys()),
            )
        else:
            logger.info(
                "bedrock guardrail headers NOT attached: guardrail_config=%r",
                guardrail,
            )

        return kwargs

    def normalize_response(self, response: Any, **kwargs) -> NormalizedResponse:
        """Normalize Anthropic response to NormalizedResponse.

        Parses content blocks (text, thinking, tool_use), maps stop_reason
        to OpenAI finish_reason, and collects reasoning_details in provider_data.
        """
        import json
        from agent.anthropic_adapter import _to_plain_data
        from agent.transports.types import ToolCall

        # Bedrock guardrail trace surfacing.  When X-Amzn-Bedrock-Trace=ENABLED
        # is sent on the request, Bedrock includes guardrail trace and
        # invocation metrics in the response.  The AnthropicBedrock SDK lets
        # these fall into pydantic's `model_extra` since the Anthropic Message
        # schema doesn't declare them.  Without explicit logging here that
        # data is dropped on the floor — costing payload bandwidth for zero
        # visibility.
        #
        # `ENABLED` only emits a trace when the guardrail intervenes, so
        # steady-state noise is zero and a log line is a real signal.
        # `ENABLED_FULL` emits on every assessment — louder, but still useful
        # for tuning thresholds.
        _log_bedrock_guardrail_trace(response)

        strip_tool_prefix = kwargs.get("strip_tool_prefix", False)
        _MCP_PREFIX = "mcp_"

        text_parts = []
        reasoning_parts = []
        reasoning_details = []
        tool_calls = []

        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "thinking":
                reasoning_parts.append(block.thinking)
                block_dict = _to_plain_data(block)
                if isinstance(block_dict, dict):
                    reasoning_details.append(block_dict)
            elif block.type == "tool_use":
                name = block.name
                if strip_tool_prefix and name.startswith(_MCP_PREFIX):
                    name = name[len(_MCP_PREFIX):]
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        name=name,
                        arguments=json.dumps(block.input),
                    )
                )

        finish_reason = self._STOP_REASON_MAP.get(response.stop_reason, "stop")

        provider_data = {}
        if reasoning_details:
            provider_data["reasoning_details"] = reasoning_details

        return NormalizedResponse(
            content="\n".join(text_parts) if text_parts else None,
            tool_calls=tool_calls or None,
            finish_reason=finish_reason,
            reasoning="\n\n".join(reasoning_parts) if reasoning_parts else None,
            usage=None,
            provider_data=provider_data or None,
        )

    def validate_response(self, response: Any) -> bool:
        """Check Anthropic response structure is valid.

        An empty content list is legitimate when ``stop_reason == "end_turn"``
        — the model's canonical way of signalling "nothing more to add" after
        a tool turn that already delivered the user-facing text. Treating it
        as invalid falsely retries a completed response.
        """
        if response is None:
            return False
        content_blocks = getattr(response, "content", None)
        if not isinstance(content_blocks, list):
            return False
        if not content_blocks:
            return getattr(response, "stop_reason", None) == "end_turn"
        return True

    def extract_cache_stats(self, response: Any) -> Optional[Dict[str, int]]:
        """Extract Anthropic cache_read and cache_creation token counts."""
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        cached = getattr(usage, "cache_read_input_tokens", 0) or 0
        written = getattr(usage, "cache_creation_input_tokens", 0) or 0
        if cached or written:
            return {"cached_tokens": cached, "creation_tokens": written}
        return None

    # Promote the adapter's canonical mapping to module level so it's shared
    _STOP_REASON_MAP = {
        "end_turn": "stop",
        "tool_use": "tool_calls",
        "max_tokens": "length",
        "stop_sequence": "stop",
        "refusal": "content_filter",
        "model_context_window_exceeded": "length",
    }

    def map_finish_reason(self, raw_reason: str) -> str:
        """Map Anthropic stop_reason to OpenAI finish_reason."""
        return self._STOP_REASON_MAP.get(raw_reason, "stop")


# Auto-register on import
from agent.transports import register_transport  # noqa: E402

register_transport("anthropic_messages", AnthropicTransport)
