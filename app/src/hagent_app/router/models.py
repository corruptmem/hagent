"""The request, the result and the error codes of an invocation."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

type InvocationStatus = Literal[
    "queued", "running", "ok", "fallback", "error", "cancelled"
]
type Path = Literal["rule", "agent_turn", "fallback"]

# Sources of design section 9.1. ``test`` is a test invocation from the
# management UI. Rules do not answer review, Assist and test invocations.
SOURCES = frozenset(
    {
        "invoke",
        "ai_task",
        "ai_task_evaluate",
        "schedule",
        "event",
        "review",
        "assist",
        "test",
    }
)
NO_RULE_SOURCES = frozenset({"review", "assist", "test"})

# Stable error codes of a result with status ``error`` or ``cancelled``.
AGENT_NOT_FOUND = "agent_not_found"
AGENT_OFF = "agent_off"
BUSY = "busy"
QUEUE_FULL = "queue_full"
CANCELLED = "cancelled"
NO_LAST_RESULT = "no_last_result"
FALLBACK_ERROR = "fallback_error"
NOT_RUNNING = "not_running"

# Fallback reasons (design section 9.7).
type FallbackReason = Literal[
    "budget",
    "provider_unreachable",
    "tool_call_limit",
    "token_limit",
    "request_limit",
    "invalid_result",
    "turn_failed",
    "runtime_unavailable",
    "app_restart",
    "app_stopped",
]


@dataclass(frozen=True, slots=True)
class InvocationRequest:
    """One invocation of one agent.

    - ``source``: one of ``SOURCES`` (design section 9.1).
    - ``text``: optional text for the agent. The agent receives it as
      ``text`` in the caller data.
    - ``data``: the optional caller data.
    - ``conversation_id``: the Assist conversation. Each conversation has
      its own queue (design section 9.6).
    - ``result_schema``: a JSON schema that replaces the result type of the
      definition for this invocation (an AI Task structure).
    - ``caller``: metadata of the source, for history only. For example the
      automation, the schedule or the event that started the invocation.
    """

    agent_id: str
    source: str
    text: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)
    conversation_id: str | None = None
    result_schema: Mapping[str, Any] | None = None
    caller: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.source not in SOURCES:
            msg = f"unknown invocation source {self.source!r}"
            raise ValueError(msg)
        if self.source == "assist" and not self.conversation_id:
            msg = "an Assist invocation needs a conversation ID"
            raise ValueError(msg)

    @property
    def source_key(self) -> str:
        """The key of the running summary and of ``last_result``.

        It matches ``TurnInput.source_key``.
        """
        if self.conversation_id:
            return f"assist:{self.conversation_id}"
        return self.source

    @property
    def queue_key(self) -> str:
        """The queue of the invocation: the agent, or the conversation."""
        if self.conversation_id:
            return f"{self.agent_id}/assist:{self.conversation_id}"
        return self.agent_id

    @property
    def turn_data(self) -> dict[str, Any]:
        """The caller data for the agent turn, with ``text``."""
        data = dict(self.data)
        if self.text is not None:
            data["text"] = self.text
        return data

    def to_json(self) -> str:
        """The JSON text that the queue row keeps."""
        return json.dumps(
            {
                "agent_id": self.agent_id,
                "source": self.source,
                "text": self.text,
                "data": dict(self.data),
                "conversation_id": self.conversation_id,
                "result_schema": (
                    None if self.result_schema is None else dict(self.result_schema)
                ),
                "caller": dict(self.caller),
            },
            ensure_ascii=False,
            default=str,
        )

    @classmethod
    def from_json(cls, text: str) -> InvocationRequest:
        """Read a request from its JSON text."""
        raw = json.loads(text)
        return cls(
            agent_id=raw["agent_id"],
            source=raw["source"],
            text=raw.get("text"),
            data=raw.get("data") or {},
            conversation_id=raw.get("conversation_id"),
            result_schema=raw.get("result_schema"),
            caller=raw.get("caller") or {},
        )


@dataclass(frozen=True, slots=True)
class InvocationResult:
    """The outcome of one invocation.

    - ``status``: ``ok`` (a rule or an agent turn answered), ``fallback``
      (the fallback answered), ``error`` or ``cancelled``.
    - ``path``: ``rule``, ``agent_turn`` or ``fallback``. ``None`` if the
      invocation did not run.
    - ``fallback_reason``: why the router used the fallback.
    - ``error_code``: a stable code for ``error`` and ``cancelled``.
    - ``cost_usd``: the cost of the provider calls of the invocation, when
      it ended. Background work after the turn (the running summary) can
      add cost later.
    """

    invocation_id: str
    agent_id: str
    status: InvocationStatus
    path: Path | None = None
    result: Any = None
    fallback_reason: str | None = None
    error_code: str | None = None
    error: str | None = None
    agent_version: int | None = None
    rule_id: str | None = None
    rule_version: int | None = None
    turn_id: str | None = None
    cost_usd: Decimal = Decimal(0)
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        """True if the caller receives a result (also from the fallback)."""
        return self.status in {"ok", "fallback"}

    def to_data(self) -> dict[str, Any]:
        """The response data of design section 9.2, with the extra fields."""
        data: dict[str, Any] = {
            "invocation_id": self.invocation_id,
            "status": self.status,
            "result": self.result,
            "writes": [],
            "path": self.path,
            "cost_usd": float(self.cost_usd),
        }
        if self.rule_id is not None:
            data["rule_id"] = self.rule_id
            data["rule_version"] = self.rule_version
        if self.fallback_reason is not None:
            data["fallback_reason"] = self.fallback_reason
        if self.error_code is not None:
            data["error"] = {"code": self.error_code, "message": self.error}
        return data
