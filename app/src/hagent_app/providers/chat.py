"""Chat model adapters (design section 16).

Pydantic AI supplies the chat models: ``OpenAIResponsesModel`` for OpenAI
and ``OpenRouterModel`` for OpenRouter. ``build_chat_model`` creates one with
the key from the ``KeySource``.

``MeteredModel`` wraps a chat model. Before each model request (also a
streamed request), it reserves the expected cost against the budgets. After
the request, it records the tokens and the cost. One agent run makes several
model requests, so the meter works per request, not per run. The agent turn
(task-10) gives a ``MeteredModel`` to its Pydantic AI agent.

The reservation assumes an uncached prompt and the full output limit:
``max_tokens`` from the model settings, or ``chat_max_output_tokens``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

import httpx2
from openai import AsyncOpenAI
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter, ModelResponse
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.providers.openrouter import OpenRouterProvider
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RequestUsage
from pydantic_core import to_json

from hagent_app.providers.config import KeySource, Provider, ProvidersConfig
from hagent_app.providers.errors import MissingKeyError
from hagent_app.providers.meter import CallSpec, Meter
from hagent_app.providers.prices import TokenUsage

# A conservative estimate of characters per token, for the reservation.
CHARS_PER_TOKEN = 3


def build_chat_model(
    provider: Provider,
    model: str,
    keys: KeySource,
    config: ProvidersConfig,
    http_client: httpx2.AsyncClient | None = None,
) -> Model:
    """Create the Pydantic AI chat model for ``provider`` and ``model``."""
    key = keys.key(provider)
    if not key:
        msg = f"no API key for {provider}"
        raise MissingKeyError(msg)
    base_url = config.base_url(provider)
    if provider == "openai":
        return OpenAIResponsesModel(
            model,
            provider=OpenAIProvider(
                base_url=base_url, api_key=key.reveal(), http_client=http_client
            ),
        )
    client = AsyncOpenAI(
        base_url=base_url, api_key=key.reveal(), http_client=http_client
    )
    return OpenRouterModel(model, provider=OpenRouterProvider(openai_client=client))


def token_usage(usage: RequestUsage) -> TokenUsage:
    """Convert a Pydantic AI ``RequestUsage`` to ``TokenUsage``."""
    return TokenUsage(
        input_tokens=usage.input_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        output_tokens=usage.output_tokens,
    )


def reported_cost(response: ModelResponse) -> Decimal | None:
    """The cost that the provider reported (OpenRouter ``usage.cost``)."""
    cost = (response.provider_details or {}).get("cost")
    if isinstance(cost, int | float) and not isinstance(cost, bool):
        return Decimal(str(cost))
    return None


class MeteredModel(WrapperModel):
    """A chat model that reserves and records the cost of each request."""

    def __init__(
        self,
        wrapped: Model,
        meter: Meter,
        *,
        provider: Provider,
        agent_id: str | None,
        max_output_tokens: int,
    ) -> None:
        super().__init__(wrapped)
        self._meter = meter
        self._vendor: Provider = provider
        self._agent_id = agent_id
        self._max_output_tokens = max_output_tokens

    def _spec(self) -> CallSpec:
        return CallSpec(
            self._agent_id,
            self._vendor,
            "chat",
            self.wrapped.model_name,
            self._vendor,
        )

    def _estimate(
        self,
        messages: Sequence[ModelMessage],
        model_settings: ModelSettings | None,
        params: ModelRequestParameters,
    ) -> TokenUsage:
        chars = len(ModelMessagesTypeAdapter.dump_json(list(messages)))
        chars += len(to_json([t.__dict__ for t in params.function_tools]))
        merged: dict[str, Any] = {**(self.settings or {}), **(model_settings or {})}
        max_tokens = merged.get("max_tokens") or self._max_output_tokens
        return TokenUsage(
            input_tokens=chars // CHARS_PER_TOKEN + 1, output_tokens=int(max_tokens)
        )

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        estimate = self._estimate(messages, model_settings, model_request_parameters)
        async with self._meter.call(self._spec(), estimate) as call:
            response = await self.wrapped.request(
                messages, model_settings, model_request_parameters
            )
            call.complete(token_usage(response.usage), reported_cost(response))
        return response

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncIterator[StreamedResponse]:
        estimate = self._estimate(messages, model_settings, model_request_parameters)
        async with (
            self._meter.call(self._spec(), estimate) as call,
            self.wrapped.request_stream(
                messages, model_settings, model_request_parameters, run_context
            ) as stream,
        ):
            try:
                yield stream
            finally:
                # Record what the stream used, also when the caller stops
                # early: the provider bills the tokens that it sent.
                response = stream.get()
                call.complete(token_usage(response.usage), reported_cost(response))

    async def batch_create(self, requests: Sequence[Any]) -> Any:
        """Batch requests bypass the meter, so ``MeteredModel`` refuses them."""
        msg = "batch requests are not metered"
        raise NotImplementedError(msg)
