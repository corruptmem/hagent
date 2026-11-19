"""Retry with backoff for chat model requests (design section 18).

``RetryingModel`` wraps a ``MeteredModel``. It retries one model request,
never a whole agent turn: a turn can change HA before a request fails, and
a new turn would repeat those changes. Each attempt goes through the meter,
so ``provider_calls`` records each attempt with its cost.

The SDK clients do not retry (``providers/chat.py``), so this is the only
retry layer.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from pydantic_ai.exceptions import (
    ModelAPIError,
    ModelConnectionError,
    ModelHTTPError,
    ModelQuotaExceededError,
    ModelRateLimitError,
    ModelRequestRejectedError,
    ModelRequestTimeout,
    ModelServerError,
    ModelUnavailableError,
)
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import RunContext

from hagent_app.providers.errors import ProviderHTTPError

_LOG = logging.getLogger(__name__)

Sleep = Callable[[float], Awaitable[None]]

_HTTP_TOO_MANY = 429
_HTTP_SERVER = 500


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How often and how long to retry.

    - ``attempts``: the maximum number of attempts, the first included.
    - ``base_delay``: the wait before the second attempt, in seconds. Each
      later wait doubles, up to ``max_delay``.
    """

    attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 20.0

    def delay(self, attempt: int) -> float:
        """The wait after failed attempt number ``attempt`` (1-based)."""
        return float(min(self.max_delay, self.base_delay * 2 ** (attempt - 1)))


def retryable(err: BaseException) -> bool:
    """True for a connection error, a timeout, a rate limit or a server error.

    A spent budget, a missing key, an unknown price, a spent provider quota
    and a rejected request are not retryable.
    """
    if isinstance(err, ProviderHTTPError):
        return err.retryable
    if not isinstance(err, ModelAPIError):
        return False
    if isinstance(err, ModelQuotaExceededError | ModelRequestRejectedError):
        return False
    if isinstance(
        err,
        ModelRateLimitError
        | ModelUnavailableError
        | ModelServerError
        | ModelConnectionError
        | ModelRequestTimeout,
    ):
        return True
    if isinstance(err, ModelHTTPError):
        return err.status_code == _HTTP_TOO_MANY or err.status_code >= _HTTP_SERVER
    return False


class RetryingModel(WrapperModel):
    """A model that retries a failed request with backoff."""

    def __init__(
        self,
        wrapped: Model,
        policy: RetryPolicy | None = None,
        *,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        super().__init__(wrapped)
        self.policy = policy or RetryPolicy()
        self._sleep = sleep

    async def _retry[T](self, attempt_fn: Callable[[], Awaitable[T]]) -> T:
        attempt = 1
        while True:
            try:
                return await attempt_fn()
            except Exception as err:
                if attempt >= self.policy.attempts or not retryable(err):
                    raise
                delay = self.policy.delay(attempt)
                _LOG.warning(
                    "model request failed (attempt %d of %d), retry in %.1f s: %s",
                    attempt,
                    self.policy.attempts,
                    delay,
                    type(err).__name__,
                )
                await self._sleep(delay)
                attempt += 1

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        return await self._retry(
            lambda: self.wrapped.request(
                messages, model_settings, model_request_parameters
            )
        )

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncIterator[StreamedResponse]:
        """A stream is not retried: the caller may have read part of it."""
        async with self.wrapped.request_stream(
            messages, model_settings, model_request_parameters, run_context
        ) as stream:
            yield stream
