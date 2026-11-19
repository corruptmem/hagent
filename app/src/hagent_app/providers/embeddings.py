"""The embedding adapter (design section 13.3).

An **embedding** is a vector of numbers that represents the meaning of a
text. Both providers accept the OpenAI embeddings request at
``POST <base>/embeddings``. The same model gives the same vectors on both,
for example ``text-embedding-3-small`` on OpenAI and
``openai/text-embedding-3-small`` on OpenRouter.

The adapter asks for ``float`` vectors. OpenRouter reports the cost of each
call. For OpenAI, the meter computes the cost from the price table.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from hagent_app.providers.config import Provider
from hagent_app.providers.errors import ProviderResponseError
from hagent_app.providers.http import ProviderHTTP
from hagent_app.providers.meter import CallSpec, Meter
from hagent_app.providers.prices import TokenUsage

# A conservative estimate of characters per token, for the reservation.
CHARS_PER_TOKEN = 3


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    """One vector per input text, in input order, and the usage."""

    model: str
    vectors: list[list[float]]
    usage: TokenUsage
    reported_cost_usd: Decimal | None = None


class EmbeddingClient(Protocol):
    """One interface for embedding calls on both providers."""

    async def embed(
        self,
        provider: Provider,
        model: str,
        texts: Sequence[str],
        *,
        agent_id: str | None,
        dimensions: int | None = None,
    ) -> EmbeddingResult:
        """Embed ``texts``. Charge the cost to ``agent_id`` (None: installation)."""


def parse_embeddings(
    body: Mapping[str, Any], count: int
) -> tuple[list[list[float]], TokenUsage, Decimal | None]:
    """Parse an embeddings response. Raise ``ProviderResponseError``."""
    usage = body.get("usage") or {}
    tokens = TokenUsage(input_tokens=int(usage.get("prompt_tokens") or 0))
    cost = usage.get("cost")
    data = body.get("data")
    if not isinstance(data, list) or len(data) != count:
        msg = f"embeddings response does not hold {count} vectors"
        raise ProviderResponseError(msg)
    vectors: list[list[float] | None] = [None] * count
    for item in data:
        index = item.get("index")
        vector = item.get("embedding")
        if not isinstance(index, int) or not 0 <= index < count:
            msg = "embeddings response has a bad index"
            raise ProviderResponseError(msg)
        if not isinstance(vector, list):
            msg = "embeddings response holds a vector that is not a float list"
            raise ProviderResponseError(msg)
        vectors[index] = [float(v) for v in vector]
    if any(v is None for v in vectors):
        msg = "embeddings response misses a vector"
        raise ProviderResponseError(msg)
    return (
        [v for v in vectors if v is not None],
        tokens,
        None if cost is None else Decimal(str(cost)),
    )


class EmbeddingAdapter:
    """Sends embedding requests to OpenAI or OpenRouter, with metering."""

    def __init__(self, http: ProviderHTTP, meter: Meter) -> None:
        self._http = http
        self._meter = meter

    async def embed(
        self,
        provider: Provider,
        model: str,
        texts: Sequence[str],
        *,
        agent_id: str | None,
        dimensions: int | None = None,
    ) -> EmbeddingResult:
        if not texts:
            return EmbeddingResult(model, [], TokenUsage())
        body: dict[str, Any] = {
            "model": model,
            "input": list(texts),
            "encoding_format": "float",
        }
        if dimensions is not None:
            body["dimensions"] = dimensions
        spec = CallSpec(agent_id, provider, "embedding", model, provider)
        chars = sum(len(t) for t in texts)
        estimate = TokenUsage(input_tokens=chars // CHARS_PER_TOKEN + len(texts))
        url = f"{self._http.base_url(provider)}/embeddings"
        async with self._meter.call(spec, estimate) as call:
            response = await self._http.post(provider, url, body)
            if not isinstance(response, Mapping):
                msg = "embeddings response is not a JSON object"
                raise ProviderResponseError(msg)
            usage = response.get("usage") or {}
            cost = usage.get("cost")
            call.complete(
                TokenUsage(input_tokens=int(usage.get("prompt_tokens") or 0)),
                None if cost is None else Decimal(str(cost)),
            )
            vectors, tokens, reported = parse_embeddings(response, len(texts))
        return EmbeddingResult(
            str(response.get("model", model)), vectors, tokens, reported
        )
