"""Anthropic adapter — official ``anthropic`` SDK, Messages API.

* Native structured output via ``output_config={"format": {"type": "json_schema"}}``
  when the model supports it (reported by the Models API); otherwise, or if the API
  refuses it, the schema is given in the system prompt and validated locally.
* Model discovery uses the Models API, which reports real context-window, output
  limit and structured-output capability data; nothing is guessed.
* No sampling parameters are forced. The SDK's retries are disabled; one shared
  retry policy applies. Base URL and key are passed explicitly (``ANTHROPIC_*``
  environment variables are never used implicitly).

No ``anthropic`` type is returned from this module.
"""

from __future__ import annotations

import time
from typing import Any, ClassVar

from pcbrouter.ai._sdk_common import SDKProvider
from pcbrouter.ai.exceptions import AIInvalidResponseError
from pcbrouter.ai.models import (
    AIModelInfo,
    ConnectionResult,
    ConnectionStatus,
    ProviderCapabilities,
)
from pcbrouter.ai.requests import AIRequest
from pcbrouter.ai.responses import AIResponse, FinishStatus, TokenUsage

_FINISH = {
    "end_turn": FinishStatus.COMPLETE,
    "stop_sequence": FinishStatus.COMPLETE,
    "max_tokens": FinishStatus.TRUNCATED,
    "model_context_window_exceeded": FinishStatus.TRUNCATED,
    "refusal": FinishStatus.REFUSED,
}


class AnthropicProvider(SDKProvider):
    PACKAGE = "anthropic"
    INSTALL_EXTRA = "anthropic"
    #: model id -> structured-output support, as reported by the Models API.
    _structured_support: ClassVar[dict[str, bool]] = {}

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            model_listing=True, native_structured_output=True, requires_api_key=True
        )

    def _default_client(self, api_key: str | None) -> Any:
        import anthropic

        return anthropic.AsyncAnthropic(
            api_key=api_key,
            base_url=self.profile.effective_base_url,
            timeout=self.profile.timeout_s,
            max_retries=0,
        )

    def _info(self, m: Any) -> AIModelInfo:
        caps = getattr(m, "capabilities", None)
        structured = getattr(getattr(caps, "structured_outputs", None), "supported", None)
        if structured is not None:
            self._structured_support[m.id] = bool(structured)
        return AIModelInfo(
            provider_kind=self.kind.value,
            model_id=m.id,
            display_name=getattr(m, "display_name", None),
            context_window=getattr(m, "max_input_tokens", None),
            max_output_tokens=getattr(m, "max_tokens", None),
            supports_structured_output=structured,
        )

    async def _list_models(self, client: Any) -> list[AIModelInfo]:
        page = await client.models.list(limit=100)
        return [self._info(m) async for m in page]

    async def _probe(self, client: Any) -> ConnectionResult:
        model = self.profile.model_id
        if model:
            info = self._info(await client.models.retrieve(model))
            return ConnectionResult(
                ConnectionStatus.CONNECTED,
                f'Connected to Anthropic. Model "{info.label}" is available.',
                True,
            )
        await client.models.list(limit=1)
        return ConnectionResult(
            ConnectionStatus.CONNECTED, "Connected to Anthropic. No model selected yet.", None
        )

    async def _generate_once(
        self, client: Any, request: AIRequest, native_schema: bool
    ) -> AIResponse:
        if native_schema and self._structured_support.get(request.model) is False:
            native_schema = False  # the Models API said this model cannot do it
        kwargs: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_output_tokens,
            "system": self.system_text(request, native_schema),
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "timeout": request.timeout_s,
        }
        if native_schema and request.response_schema is not None:
            kwargs["output_config"] = {
                "format": {"type": "json_schema", "schema": request.response_schema}
            }
        start = time.perf_counter()
        msg = await client.messages.create(**kwargs)
        latency = time.perf_counter() - start
        if isinstance(msg, str | bytes) or not hasattr(msg, "content"):
            raise AIInvalidResponseError("Anthropic returned a non-message body (proxy/gateway?)")
        text = "".join(
            getattr(block, "text", "")
            for block in (getattr(msg, "content", None) or [])
            if getattr(block, "type", None) == "text"
        )
        usage = getattr(msg, "usage", None)
        stop = getattr(msg, "stop_reason", None)
        return AIResponse(
            request_id=request.request_id,
            provider=self.kind.value,
            model=getattr(msg, "model", None) or request.model,
            content=text,
            usage=(
                TokenUsage(
                    getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None)
                )
                if usage
                else None
            ),
            latency_s=latency,
            finish_status=_FINISH.get(str(stop), FinishStatus.UNKNOWN),
            used_native_schema=native_schema,
            raw_metadata={"message_id": str(getattr(msg, "id", "")), "stop_reason": str(stop)},
        )
