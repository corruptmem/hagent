"""Helpers for invocations through the link: errors and attachments."""

from __future__ import annotations

import base64
import mimetypes
from collections.abc import Iterable, Mapping
from typing import Any

from homeassistant.components.conversation import Attachment
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import MAX_ATTACHMENT_BYTES
from .link import LinkCommandError, LinkError

# The types that the app accepts (see docs/link.md).
ATTACHMENT_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "application/pdf",
        "text/plain",
        "text/markdown",
        "text/csv",
        "application/json",
    }
)


class HagentInvocationError(HomeAssistantError):
    """An invocation failed: the app is unreachable or returned an error."""


def link_error(err: LinkError) -> HagentInvocationError:
    """An HA error for a link failure."""
    if isinstance(err, LinkCommandError):
        return HagentInvocationError(f"hagent: {err.code}: {err.message}")
    return HagentInvocationError(f"hagent app unreachable: {err}")


def result_data(data: Mapping[str, Any]) -> Any:
    """The result of an invocation. Raise if its status is error or cancelled."""
    status = data.get("status")
    if status in {"error", "cancelled"}:
        error = data.get("error") or {}
        code = error.get("code", status)
        msg = f"hagent: {code}: {error.get('message', '')}"
        raise HagentInvocationError(msg)
    return data.get("result")


def read_attachments(items: Iterable[Attachment]) -> list[dict[str, str]]:
    """Read HA attachments into link attachments. Run it in an executor.

    Raise ``HagentInvocationError`` for an unsupported type or when the sum
    of the sizes is above the app limit.
    """
    result: list[dict[str, str]] = []
    total = 0
    for item in items:
        mime = item.mime_type or mimetypes.guess_type(item.path.name)[0] or ""
        if mime not in ATTACHMENT_TYPES:
            msg = f"hagent does not accept attachments of type {mime!r}"
            raise HagentInvocationError(msg)
        data = item.path.read_bytes()
        total += len(data)
        if total > MAX_ATTACHMENT_BYTES:
            msg = f"the attachments are larger than {MAX_ATTACHMENT_BYTES} bytes"
            raise HagentInvocationError(msg)
        result.append(
            {
                "name": item.path.name,
                "mime_type": mime,
                "data": base64.b64encode(data).decode("ascii"),
            }
        )
    return result


async def async_read_attachments(
    hass: HomeAssistant, items: Iterable[Attachment] | None
) -> list[dict[str, str]]:
    """Read attachments in the executor."""
    if not items:
        return []
    return await hass.async_add_executor_job(read_attachments, list(items))
