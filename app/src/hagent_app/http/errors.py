"""JSON errors for the HTTP API.

Every error response has this body::

    {"error": {"code": "<stable code>", "message": "<text>", "details": ...}}

A handler raises ``ApiError``. The error middleware of ``HttpServer``
converts it, and every ``web.HTTPException``, to this format.
"""

from __future__ import annotations

import json
from typing import Any

from aiohttp import web


class ApiError(Exception):
    """An error that the API returns with an HTTP status."""

    def __init__(
        self, status: int, code: str, message: str, details: Any = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


def error_response(
    status: int, code: str, message: str, details: Any = None
) -> web.Response:
    """A JSON error response."""
    body: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        body["details"] = details
    return web.json_response({"error": body}, status=status)


async def read_json(request: web.Request) -> Any:
    """The parsed JSON body. Raise ``ApiError`` 400 if it is not JSON."""
    try:
        return json.loads(await request.read())
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        raise ApiError(400, "invalid_json", f"the body is not JSON: {err}") from err
