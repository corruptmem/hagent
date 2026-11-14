"""Errors of the providers area.

The adapters raise these errors. They do not retry. The runtime decides on
retries and backoff (design section 18).
"""

from __future__ import annotations


class ProviderError(Exception):
    """A provider call failed."""


class MissingKeyError(ProviderError):
    """No API key is set for the provider."""


class ProviderHTTPError(ProviderError):
    """The provider returned an HTTP error, or the connection failed.

    ``status`` is ``None`` for a connection error. The message never holds
    an API key.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def retryable(self) -> bool:
        """True for a connection error, a rate limit or a server error."""
        return self.status is None or self.status == 429 or self.status >= 500  # noqa: PLR2004


class ProviderResponseError(ProviderError):
    """The provider response does not have the expected shape."""


class PriceUnknownError(ProviderError):
    """No price is known for the model, so the cost cannot be reserved."""


class BudgetExceededError(ProviderError):
    """The reservation does not fit in the agent or installation budget.

    ``scope`` is ``"agent"`` or ``"installation"``.
    """

    def __init__(self, message: str, scope: str) -> None:
        super().__init__(message)
        self.scope = scope
