"""Thin client for the Home Assistant REST API."""

from __future__ import annotations

from typing import Any, cast

import httpx

JSON = dict[str, Any]


class HomeAssistantError(Exception):
    """Home Assistant returned an error or an unexpected response."""


class HomeAssistantClient:
    """Calls the Home Assistant REST API with a long-lived access token."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._http = httpx.Client(
            base_url=f"{base_url.rstrip('/')}/api",
            headers={"Authorization": f"Bearer {token}"},
            transport=transport,
            timeout=timeout,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> HomeAssistantClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _request(self, method: str, path: str, json: JSON | None = None) -> Any:
        try:
            response = self._http.request(method, path, json=json)
        except httpx.HTTPError as err:
            raise HomeAssistantError(f"{method} {path} failed: {err}") from err
        if response.status_code >= 400:
            raise HomeAssistantError(
                f"{method} {path} returned {response.status_code}: {response.text}"
            )
        return response.json()

    def ping(self) -> str:
        """Return the API status message. Fails if the token is not valid."""
        return str(self._request("GET", "/")["message"])

    def list_states(self) -> list[JSON]:
        return cast(list[JSON], self._request("GET", "/states"))

    def get_state(self, entity_id: str) -> JSON:
        return cast(JSON, self._request("GET", f"/states/{entity_id}"))

    def list_services(self) -> list[JSON]:
        return cast(list[JSON], self._request("GET", "/services"))

    def call_service(
        self, domain: str, service: str, data: JSON | None = None
    ) -> list[JSON]:
        """Call a service. This can change physical devices."""
        return cast(
            list[JSON],
            self._request("POST", f"/services/{domain}/{service}", json=data or {}),
        )
