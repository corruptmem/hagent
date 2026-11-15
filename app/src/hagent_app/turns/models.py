"""The input, the result and the history records of an agent turn."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

type TurnStatus = Literal["running", "ok", "stopped", "failed", "cancelled"]
type StopReason = Literal[
    "tool_call_limit",
    "token_limit",
    "request_limit",
    "budget",
    "provider_error",
    "invalid_result",
    "error",
    "cancelled",
]
type CompactionKind = Literal[
    "tool_result_cut", "in_turn", "running_summary", "main_file_request"
]


# Attachments (design section 9.3, AI Task ``SUPPORT_ATTACHMENTS``). The
# model receives images and PDF files as binary parts, and text files as
# text. Other types are refused before the invocation starts.
ATTACHMENT_BINARY_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf"}
)
ATTACHMENT_TEXT_TYPES = frozenset(
    {"text/plain", "text/markdown", "text/csv", "application/json"}
)
ATTACHMENT_TYPES = ATTACHMENT_BINARY_TYPES | ATTACHMENT_TEXT_TYPES
# The sum of the attachment sizes of one invocation.
ATTACHMENT_MAX_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class Attachment:
    """One file that goes to the model with the invocation."""

    name: str
    mime_type: str
    data: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if self.mime_type not in ATTACHMENT_TYPES:
            msg = f"attachment type {self.mime_type!r} is not supported"
            raise ValueError(msg)

    @property
    def summary(self) -> dict[str, Any]:
        """The name, type and size, for the caller data and history."""
        return {"name": self.name, "mime_type": self.mime_type, "size": len(self.data)}


def check_attachments(attachments: tuple[Attachment, ...]) -> None:
    """Raise ``ValueError`` if the attachments are larger than the limit."""
    total = sum(len(a.data) for a in attachments)
    if total > ATTACHMENT_MAX_BYTES:
        msg = (
            f"the attachments have {total} bytes; the limit is "
            f"{ATTACHMENT_MAX_BYTES} bytes"
        )
        raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class TurnInput:
    """One invocation that the router gives to an agent turn.

    - ``source``: where the invocation comes from, for example ``invoke``,
      ``ai_task``, ``schedule``, ``event``, ``review`` or ``assist``
      (design section 9.1).
    - ``inputs``: the current values of the input entities. The router
      collects them (design section 9.4).
    - ``data``: the optional data from the caller.
    - ``conversation_id``: the Assist conversation, if any. Each conversation
      has its own running summary.
    - ``result_schema``: a JSON schema that replaces the result type of the
      definition for this invocation (an AI Task structure, section 9.3).
    - ``dry_run``: a test invocation. Tools check and record changes but do
      not make them. The turn does not go into the running summaries.
    - ``attachments``: files for the model (AI Task attachments). The caller
      data holds their names; the model receives their content.
    """

    source: str
    inputs: Mapping[str, Any] = field(default_factory=dict)
    data: Mapping[str, Any] = field(default_factory=dict)
    invocation_id: str | None = None
    conversation_id: str | None = None
    result_schema: Mapping[str, Any] | None = None
    dry_run: bool = False
    now: datetime | None = None
    attachments: tuple[Attachment, ...] = ()

    @property
    def source_key(self) -> str:
        """The key of the running summary for this invocation."""
        if self.conversation_id:
            return f"assist:{self.conversation_id}"
        return self.source


@dataclass(frozen=True, slots=True)
class TurnUsage:
    """The token counts of a turn. ``input_tokens`` includes cached tokens."""

    requests: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True, slots=True)
class TurnResult:
    """The outcome of an agent turn, for the router.

    ``status`` is ``ok`` when the model returned a valid result. For
    ``stopped`` (a per-turn limit) and ``failed``, ``stop_reason`` says why,
    and the router returns the fallback (design section 9.7). ``result`` is
    ``None`` for the result type ``none``.
    """

    turn_id: str
    status: TurnStatus
    result: Any = None
    stop_reason: StopReason | None = None
    error: str | None = None
    usage: TurnUsage = TurnUsage()

    @property
    def ok(self) -> bool:
        """True if the turn returned a result."""
        return self.status == "ok"


@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    """One tool call of a turn, with its full result."""

    seq: int
    created_at: str
    tool_call_id: str | None
    tool: str
    arguments: Any
    ok: bool
    error_code: str | None
    result: Any
    result_chars: int
    cut: bool
    audit_id: int | None


@dataclass(frozen=True, slots=True)
class CompactionRecord:
    """One compaction (design section 13.1)."""

    id: int
    turn_id: str
    created_at: str
    kind: CompactionKind
    detail: Any
    summary: str | None


@dataclass(frozen=True, slots=True)
class TurnRecord:
    """One agent turn, as history keeps it.

    ``tool_call_records`` and ``compactions`` are empty in lists, and filled
    by ``TurnHistory.get``. ``messages`` is the Pydantic AI message JSON.
    """

    id: str
    agent_id: str
    agent_version: int
    invocation_id: str | None
    source: str
    source_key: str
    conversation_id: str | None
    dry_run: bool
    started_at: str
    finished_at: str | None
    status: TurnStatus
    stop_reason: str | None
    error: str | None
    input: Any
    instructions_sha256: str
    prompt: str
    result: Any
    usage: TurnUsage
    messages: Any = None
    tool_call_records: tuple[ToolCallRecord, ...] = ()
    compactions: tuple[CompactionRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class SourceSummary:
    """The running summary of one source of an agent."""

    agent_id: str
    source_key: str
    summary: str
    covers_until: str
    turn_count: int
    updated_at: str
