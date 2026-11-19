"""Helpers for invocations through the link: errors and attachments."""

from __future__ import annotations

import base64
import mimetypes
import os
import stat
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from homeassistant.components.conversation import Attachment
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError, Unauthorized, UnknownUser

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


async def async_check_admin(hass: HomeAssistant, context: Context | None) -> None:
    """Apply the admin rule of HA admin services to one call.

    A call without a user (an automation, a script that no user started, a
    voice satellite) may invoke an agent. A call by an admin user may invoke
    an agent. A call by any other user gets ``Unauthorized``. This is the
    rule of ``async_register_admin_service`` (see docs/link.md).
    """
    if context is None or not context.user_id:
        return
    user = await hass.auth.async_get_user(context.user_id)
    if user is None:
        raise UnknownUser(context=context)
    if not user.is_admin:
        raise Unauthorized(context=context)


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
        data = _read_bounded(item.path, MAX_ATTACHMENT_BYTES - total)
        total += len(data)
        result.append(
            {
                "name": item.path.name,
                "mime_type": mime,
                "data": base64.b64encode(data).decode("ascii"),
            }
        )
    return result


def _too_large() -> HagentInvocationError:
    return HagentInvocationError(
        f"the attachments are larger than {MAX_ATTACHMENT_BYTES} bytes"
    )


def _check_file(path: Path, info: os.stat_result, limit: int) -> None:
    if not stat.S_ISREG(info.st_mode):
        msg = f"hagent accepts only regular files as attachments: {path.name}"
        raise HagentInvocationError(msg)
    if info.st_size > limit:
        raise _too_large()


def _read_bounded(path: Path, limit: int) -> bytes:
    """Read a regular file of at most ``limit`` bytes.

    Check the size before the read, and read at most ``limit + 1`` bytes, so
    that a large file never goes fully into memory. Refuse a file that is
    not a regular file (for example a FIFO, which can block the read).
    """
    try:
        # stat() first: open() on a FIFO blocks until a writer appears.
        _check_file(path, path.stat(), limit)
        with path.open("rb") as file:
            _check_file(path, os.fstat(file.fileno()), limit)
            data = file.read(limit + 1)
    except OSError as err:
        msg = f"hagent cannot read the attachment {path.name}: {err.strerror}"
        raise HagentInvocationError(msg) from err
    if len(data) > limit:
        raise _too_large()
    return data


async def async_read_attachments(
    hass: HomeAssistant, items: Iterable[Attachment] | None
) -> list[dict[str, str]]:
    """Read attachments in the executor."""
    if not items:
        return []
    return await hass.async_add_executor_job(read_attachments, list(items))
