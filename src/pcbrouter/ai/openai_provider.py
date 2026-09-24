"""OpenAI adapter — official ``openai`` SDK, **Responses API**.

* Native structured output via ``text.format = {"type": "json_schema", "strict": true}``.
* ``store=False``: responses are not retained by OpenAI for later retrieval.
* No sampling parameters are forced (several current models reject them).
* The SDK's own retries are disabled; :mod:`pcbrouter.ai.retry` applies one policy.
* Base URL and key are passed explicitly, so ``OPENAI_*`` environment variables can
  never redirect requests or supply an unconfigured key.

No ``openai`` type is returned from this module.
"""

from __future__ import annotations

from typing import Any

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


class OpenAIProvider(SDKProvider):
    PACKAGE = "openai"
    INSTALL_EXTRA = "openai"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            model_listing=True, native_structured_output=True, requires_api_key=True
        )

    def _default_client(self, api_key: str | None) -> Any:
        import openai

        return openai.AsyncOpenAI(
            api_key=api_key,
            base_url=self.profile.effective_base_url,
            organization=self.profile.organization or None,
            project=self.profile.project or None,
            timeout=self.profile.timeout_s,
            max_retries=0,
        )

    async def _list_models(self, client: Any) -> list[AIModelInfo]:
        page = await client.models.list()
        # OpenAI's listing reports ids only: every capability stays unknown (None).
        return [
            AIModelInfo(
                provider_kind=self.kind.value,
                model_id=m.id,
                metadata={"owned_by": getattr(m, "owned_by", None)},
            )
            async for m in page
        ]

    async def _probe(self, client: Any) -> ConnectionResult:
        model = self.profile.model_id
        if model:
            await client.models.retrieve(model)  # 404 -> model unavailable
            return ConnectionResult(
                ConnectionStatus.CONNECTED,
                f'Connected to OpenAI. Model "{model}" is available.',
                True,
            )
        await client.models.list()
        return ConnectionResult(
            ConnectionStatus.CONNECTED, "Connected to OpenAI. No model selected yet.", None
        )

    async def _generate_once(
        self, client: Any, request: AIRequest, native_schema: bool
    ) -> AIResponse:
        import time

        kwargs: dict[str, Any] = {
            "model": request.model,
            "instructions": self.system_text(request, native_schema),
            "input": [{"role": m.role, "content": m.content} for m in request.messages],
            "max_output_tokens": request.max_output_tokens,
            "store": False,
            "timeout": request.timeout_s,
        }
        if native_schema and request.response_schema is not None:
            kwargs["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": request.schema_name,
                    "schema": request.response_schema,
                    "strict": True,
                }
            }
        start = time.perf_counter()
        resp = await client.responses.create(**kwargs)
        latency = time.perf_counter() - start
        if isinstance(resp, str | bytes) or not hasattr(resp, "output"):
            raise AIInvalidResponseError("OpenAI returned a non-response body (proxy/gateway?)")

        status = getattr(resp, "status", None)
        if status == "failed":
            error = getattr(resp, "error", None)
            raise AIInvalidResponseError(
                f"OpenAI response failed: {getattr(error, 'message', error)}"
            )
        refusal = None
        for item in getattr(resp, "output", None) or []:
            if getattr(item, "type", None) == "message":
                for content in getattr(item, "content", None) or []:
                    if getattr(content, "type", None) == "refusal":
                        refusal = getattr(content, "refusal", "") or "refused"
        text = resp.output_text or ""
        if refusal is not None:
            finish = FinishStatus.REFUSED
            text = text or refusal
        elif status == "incomplete":
            finish = FinishStatus.TRUNCATED
        elif status == "completed":
            finish = FinishStatus.COMPLETE
        else:
            finish = FinishStatus.UNKNOWN
        usage = getattr(resp, "usage", None)
        return AIResponse(
            request_id=request.request_id,
            provider=self.kind.value,
            model=getattr(resp, "model", None) or request.model,
            content=text,
            usage=(
                TokenUsage(
                    getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None)
                )
                if usage
                else None
            ),
            latency_s=latency,
            finish_status=finish,
            used_native_schema=native_schema,
            raw_metadata={"response_id": str(getattr(resp, "id", "")), "status": str(status)},
        )
