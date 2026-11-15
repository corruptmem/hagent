"""Messages of the integration link, version 1. See ``docs/link.md``.

Each message is one JSON object in one WebSocket text frame. ``type`` names
the message. A command from the integration has an integer ``id``. The app
answers each command with one ``response`` message with the same ``id``.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from hagent_app.agents import Agent
from hagent_app.router.models import InvocationRequest, InvocationResult
from hagent_app.turns.models import Attachment

PROTOCOL_VERSION = 1
LINK_PATH = "/api/link/ws"
# Attachments travel as base64 inside an invoke command. The app accepts
# 8 MiB of attachments (``ATTACHMENT_MAX_BYTES``), which is 11 MiB as base64.
MAX_MESSAGE_BYTES = 16 * 1024 * 1024
DEFAULT_TIMEOUT = 30.0
MAX_TIMEOUT = 3600.0
# The integration can invoke from these sources only.
LINK_SOURCES = frozenset({"invoke", "ai_task", "ai_task_evaluate", "assist"})
# The longest JSON text of a last result in an agent snapshot.
LAST_RESULT_MAX_CHARS = 16_000

# Agent status values (design section 6).
STATUS_IDLE = "idle"
STATUS_RUNNING = "running"
STATUS_BUDGET = "budget_spent"
STATUS_OFF = "off"
STATUS_ERROR = "error"

# Error codes of the link itself. Router codes pass through unchanged.
INVALID_MESSAGE = "invalid_message"
UNKNOWN_COMMAND = "unknown_command"
TIMEOUT = "timeout"
NOT_FOUND = "not_found"


class LinkError(Exception):
    """A command failed. The app sends ``code`` and the message back."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class InvokeCommand:
    """A parsed ``invoke`` command."""

    request: InvocationRequest
    wait: bool
    timeout: float
    on_timeout: str | None


def response(msg_id: int, data: Any) -> dict[str, Any]:
    """A successful response."""
    return {"type": "response", "id": msg_id, "success": True, "data": data}


def error_response(msg_id: int | None, code: str, message: str) -> dict[str, Any]:
    """A failed response."""
    return {
        "type": "response",
        "id": msg_id,
        "success": False,
        "error": {"code": code, "message": message},
    }


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise LinkError(INVALID_MESSAGE, f"{name!r} must be an object")
    return dict(value)


def _attachments(value: Any) -> tuple[Attachment, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise LinkError(INVALID_MESSAGE, "'attachments' must be a list")
    items = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise LinkError(INVALID_MESSAGE, "each attachment must be an object")
        try:
            data = base64.b64decode(str(raw["data"]), validate=True)
            items.append(Attachment(str(raw["name"]), str(raw["mime_type"]), data))
        except (KeyError, binascii.Error) as err:
            msg = "an attachment needs name, mime_type and base64 data"
            raise LinkError(INVALID_MESSAGE, msg) from err
        except ValueError as err:
            raise LinkError(INVALID_MESSAGE, str(err)) from err
    return tuple(items)


def parse_invoke(msg: Mapping[str, Any]) -> InvokeCommand:
    """Parse an ``invoke`` command. Raise ``LinkError`` if it is not valid."""
    agent_id = msg.get("agent_id")
    if not isinstance(agent_id, str) or not agent_id:
        raise LinkError(INVALID_MESSAGE, "'agent_id' must be a text")
    source = msg.get("source", "invoke")
    if source not in LINK_SOURCES:
        raise LinkError(INVALID_MESSAGE, f"source {source!r} is not allowed")
    text = msg.get("text")
    if text is not None and not isinstance(text, str):
        raise LinkError(INVALID_MESSAGE, "'text' must be a text")
    conversation_id = msg.get("conversation_id")
    if conversation_id is not None and not isinstance(conversation_id, str):
        raise LinkError(INVALID_MESSAGE, "'conversation_id' must be a text")
    schema = msg.get("result_schema")
    result_schema = None if schema is None else _mapping(schema, "result_schema")
    wait = msg.get("wait", True)
    if not isinstance(wait, bool):
        raise LinkError(INVALID_MESSAGE, "'wait' must be true or false")
    timeout = msg.get("timeout", DEFAULT_TIMEOUT)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int | float)
        or not 0 < timeout <= MAX_TIMEOUT
    ):
        raise LinkError(
            INVALID_MESSAGE, f"'timeout' must be a number from 0 to {MAX_TIMEOUT:g}"
        )
    on_timeout = msg.get("on_timeout")
    if on_timeout not in {None, "cancel", "continue"}:
        raise LinkError(INVALID_MESSAGE, "'on_timeout' must be cancel or continue")
    try:
        request = InvocationRequest(
            agent_id,
            source,
            text=text,
            data=_mapping(msg.get("data"), "data"),
            conversation_id=conversation_id,
            result_schema=result_schema,
            caller=_mapping(msg.get("caller"), "caller"),
            attachments=_attachments(msg.get("attachments")),
        )
    except ValueError as err:
        raise LinkError(INVALID_MESSAGE, str(err)) from err
    return InvokeCommand(request, wait, float(timeout), on_timeout)


def clip_result(value: Any) -> tuple[Any, bool]:
    """Return the value, or ``None`` and true if its JSON text is too long."""
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) > LAST_RESULT_MAX_CHARS:
        return None, True
    return value, False


def last_result_data(result: InvocationResult, finished_at: str) -> dict[str, Any]:
    """The ``last_result`` object of an agent snapshot."""
    value, clipped = clip_result(result.result)
    data: dict[str, Any] = {
        "invocation_id": result.invocation_id,
        "status": result.status,
        "path": result.path,
        "result": value,
        "finished_at": finished_at,
    }
    if clipped:
        data["result_clipped"] = True
    if result.error_code is not None:
        data["error"] = {"code": result.error_code, "message": result.error}
    return data


def agent_snapshot(
    agent: Agent,
    *,
    status: str,
    running: int,
    cost_today_usd: float | None,
    last_result: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """The ``agent`` object of the ``hello`` and ``agent`` messages."""
    definition = agent.definition
    return {
        "id": agent.id,
        "name": agent.name,
        "enabled": agent.enabled,
        "version": agent.version,
        "assist": definition.assist.enabled,
        "has_outputs": bool(definition.outputs),
        "result_type": definition.result.type,
        "status": status,
        "running": running,
        "cost_today_usd": cost_today_usd,
        "last_result": None if last_result is None else dict(last_result),
    }
