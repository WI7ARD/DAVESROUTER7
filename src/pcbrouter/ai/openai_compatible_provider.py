"""Generic adapter for OpenAI-compatible servers (LM Studio, Ollama, vLLM, llama.cpp,
hosted gateways) using the Chat Completions protocol via the ``openai`` SDK.

Compatible servers implement different subsets of the API, so nothing is assumed:

* API key optional (a placeholder is sent when none is configured — local servers
  ignore it).
* ``GET /models`` may be missing → model listing reports "unsupported" and the user
  enters a model ID; the connection test then falls back to a 1-token completion.
* Response-format support is probed with a ladder: ``json_schema`` → ``json_object``
  → plain prompt instructions. The first level that works is remembered per
  endpoint+model for this session. Local validation happens in every case.
"""

from __future__ import annotations

import time
from typing import Any, ClassVar

from pcbrouter.ai._sdk_common import SDKProvider, map_sdk_error
from pcbrouter.ai.exceptions import (
    AICapabilityError,
    AIInvalidRequestError,
    AIInvalidResponseError,
    AIModelNotFoundError,
    AIProviderError,
)
from pcbrouter.ai.models import (
    AIModelInfo,
    ConnectionResult,
    ConnectionStatus,
    ProviderCapabilities,
)
from pcbrouter.ai.requests import AIRequest
from pcbrouter.ai.responses import AIResponse, FinishStatus, TokenUsage

NO_KEY_PLACEHOLDER = "not-required"
_LEVELS = ("json_schema", "json_object", "none")


class OpenAICompatibleProvider(SDKProvider):
    PACKAGE = "openai"
    INSTALL_EXTRA = "openai"
    _format_level: ClassVar[dict[tuple[str, str], str]] = {}

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            model_listing=True,
            native_structured_output=True,
            requires_api_key=self.profile.requires_api_key,
            custom_base_url=True,
        )

    def _default_client(self, api_key: str | None) -> Any:
        import openai

        return openai.AsyncOpenAI(
            api_key=api_key or NO_KEY_PLACEHOLDER,
            base_url=self.profile.effective_base_url,
            timeout=self.profile.timeout_s,
            max_retries=0,
        )

    async def _list_models(self, client: Any) -> list[AIModelInfo]:
        try:
            page = await client.models.list()
            return [AIModelInfo(provider_kind=self.kind.value, model_id=m.id) async for m in page]
        except Exception as exc:
            err = map_sdk_error(exc, self.profile.name)
            if isinstance(err, AIModelNotFoundError | AIInvalidRequestError):
                raise AICapabilityError(
                    f"{self.profile.name}: model listing unsupported ({err})",
                    user_message="This endpoint does not support model listing. "
                    "Enter the model ID manually.",
                ) from exc
            raise err from exc

    async def _probe(self, client: Any) -> ConnectionResult:
        model = self.profile.model_id
        try:
            models = await self._list_models(client)
        except AICapabilityError:
            if not model:
                return ConnectionResult(
                    ConnectionStatus.ERROR,
                    "Endpoint reachable, but it cannot list models. Enter a "
                    "model ID and test again.",
                )
            # Smallest practical generation; no board data is sent.
            await client.chat.completions.create(
                model=model, messages=[{"role": "user", "content": "ping"}], max_tokens=1
            )
            return ConnectionResult(
                ConnectionStatus.CONNECTED,
                f'Connected. Model "{model}" responded (model listing '
                "is not supported by this endpoint).",
                True,
            )
        if not model:
            return ConnectionResult(
                ConnectionStatus.CONNECTED, f"Connected. {len(models)} model(s) reported.", None
            )
        if any(m.model_id == model for m in models):
            return ConnectionResult(
                ConnectionStatus.CONNECTED, f'Connected. Model "{model}" is available.', True
            )
        return ConnectionResult(
            ConnectionStatus.MODEL_UNAVAILABLE,
            f'Connected, but model "{model}" is not listed by the endpoint.',
            False,
        )

    async def _generate_once(
        self, client: Any, request: AIRequest, native_schema: bool
    ) -> AIResponse:
        key = (self.profile.effective_base_url, request.model)
        start_level = self._format_level.get(key, "json_schema" if native_schema else "none")
        levels = _LEVELS[_LEVELS.index(start_level) :]
        last_error: AIProviderError | None = None
        for level in levels:
            try:
                response = await self._complete(client, request, level)
            except Exception as exc:
                err = map_sdk_error(exc, self.profile.name)
                if "think" in str(exc).lower() and isinstance(err, AIProviderError):
                    # The receiver rejected the "think" flag itself (unknown
                    # field on strict mocks/servers, not the format): retry this
                    # level once without it.
                    try:
                        response = await self._complete(client, request, level, think=False)
                    except Exception as exc2:
                        err = map_sdk_error(exc2, self.profile.name)
                    else:
                        self._format_level[key] = level
                        return response
                if level != "none" and isinstance(err, AICapabilityError | AIInvalidRequestError):
                    last_error = err
                    continue  # try the next, less demanding response format
                raise err from exc
            self._format_level[key] = level
            return response
        assert last_error is not None  # pragma: no cover
        raise last_error

    async def _complete(
        self, client: Any, request: AIRequest, level: str, think: bool = True
    ) -> AIResponse:
        native = level == "json_schema"
        messages: list[dict[str, str]] = [
            {"role": "system", "content": self.system_text(request, native)}
        ]
        messages += [{"role": m.role, "content": m.content} for m in request.messages]
        kwargs: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "max_tokens": request.max_output_tokens,
            "timeout": request.timeout_s,
        }
        if think:
            kwargs.update(
                {
                    # Reasoning ("thinking") models spend the token budget narrating
                    # their thoughts before answering, which starves structured
                    # commands and blows past timeouts. Disable it where the server
                    # honours the flag; servers that reject it fall back below
                    # (the flag is dropped).
                    "think": False,
                }
            )
        if level == "json_schema" and request.response_schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema_name,
                    "schema": request.response_schema,
                    "strict": True,
                },
            }
        elif level == "json_object":
            kwargs["response_format"] = {"type": "json_object"}
        start = time.perf_counter()
        completion = await client.chat.completions.create(**kwargs)
        latency = time.perf_counter() - start
        if isinstance(completion, str | bytes) or not hasattr(completion, "choices"):
            raise AIInvalidResponseError(f"{self.profile.name}: not a chat-completion response")
        choices = getattr(completion, "choices", None) or []
        if not choices:
            raise AIInvalidResponseError(f"{self.profile.name}: response has no choices")
        message = choices[0].message
        refusal = getattr(message, "refusal", None)
        text = getattr(message, "content", None) or refusal or ""
        finish_reason = getattr(choices[0], "finish_reason", None)
        finish = (
            FinishStatus.REFUSED
            if refusal
            else (
                FinishStatus.TRUNCATED
                if finish_reason == "length"
                else FinishStatus.COMPLETE if finish_reason == "stop" else FinishStatus.UNKNOWN
            )
        )
        usage = getattr(completion, "usage", None)
        return AIResponse(
            request_id=request.request_id,
            provider=self.kind.value,
            model=getattr(completion, "model", None) or request.model,
            content=text,
            usage=(
                TokenUsage(
                    getattr(usage, "prompt_tokens", None), getattr(usage, "completion_tokens", None)
                )
                if usage
                else None
            ),
            latency_s=latency,
            finish_status=finish,
            used_native_schema=native,
            raw_metadata={"response_format": level, "finish_reason": str(finish_reason)},
        )
