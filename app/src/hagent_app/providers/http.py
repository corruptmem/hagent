"""Shared HTTP code for the direct provider calls (decisions, embeddings).

All provider traffic uses httpx2, the HTTP client of the OpenAI Python
client. Tests replace the transport with ``httpx2.MockTransport``. The key
goes only into the ``Authorization`` header of a request to the provider.
Error messages pass through ``redact``, so they never hold a key.
"""

from __future__ import annotations

from typing import Any

import httpx2

from hagent_app.providers.config import KeySource, Provider, ProvidersConfig, redact
from hagent_app.providers.errors import (
    MissingKeyError,
    ProviderHTTPError,
    ProviderResponseError,
)

DEFAULT_TIMEOUT = httpx2.Timeout(60.0, connect=10.0)
MAX_ERROR_TEXT = 500


class ProviderHTTP:
    """Sends JSON POST requests to a provider API."""

    def __init__(
        self,
        config: ProvidersConfig,
        keys: KeySource,
        client: httpx2.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._keys = keys
        self.client = client or httpx2.AsyncClient(timeout=DEFAULT_TIMEOUT)

    def base_url(self, provider: Provider) -> str:
        return self._config.base_url(provider)

    async def post(self, provider: Provider, url: str, body: dict[str, Any]) -> Any:
        """POST ``body`` to ``url`` and return the parsed JSON response."""
        key = self._keys.key(provider)
        if not key:
            msg = f"no API key for {provider}"
            raise MissingKeyError(msg)
        headers = {"Authorization": f"Bearer {key.reveal()}"}
        try:
            response = await self.client.post(url, json=body, headers=headers)
        except httpx2.HTTPError as err:
            text = redact(f"{type(err).__name__}: {err}", self._keys)
            msg = f"{provider} request failed: {text}"
            raise ProviderHTTPError(msg) from None
        if response.status_code >= 400:  # noqa: PLR2004
            text = redact(response.text[:MAX_ERROR_TEXT], self._keys)
            msg = f"{provider} returned HTTP {response.status_code}: {text}"
            raise ProviderHTTPError(msg, response.status_code)
        try:
            return response.json()
        except ValueError:
            msg = f"{provider} returned a response that is not JSON"
            raise ProviderResponseError(msg) from None

    async def aclose(self) -> None:
        await self.client.aclose()
