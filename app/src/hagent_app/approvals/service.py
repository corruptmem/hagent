"""The approval flow: hold a tool call until an approver answers.

Terms:

- **Approval request**: one held tool call. It has a random 128-bit ID.
- **Answer**: accept or reject, from an action button of the notification or
  from the management API.
- **Action event**: the ``mobile_app_notification_action`` event. The HA
  Companion app fires it when a person taps an action button. Its
  ``context.user_id`` is the HA user of the registration that fired it.

Rules (design section 8, task-4 findings):

- Only the HA users in the approver list can answer. An answer from another
  user, from no user, or from the HA user of the app itself has no effect.
  The app records it in ``approval_answers``.
- The app listens only to the action event, never to the legacy
  ``ios.*`` events, so that one tap never counts twice.
- A tap on the notification body is no answer. The request stays pending.
- One conditional ``UPDATE ... WHERE status = 'pending'`` decides each
  request. The first valid answer wins. A timeout, a cancel and the app
  stop use the same statement.
- A timeout counts as a rejection (``approval_timeout``).
- If no notification reaches an approver, the request fails at once.
- After the end of a request, the app clears the notification on every
  target, with the tag.
- When the app starts, it expires every pending request. The agent turn
  that waited for it no longer exists.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from hagent_app.approvals.config import ApprovalsConfig
from hagent_app.approvals.notifier import ApprovalMessage, Delivery, Notifier
from hagent_app.http.events import ChangeEvents
from hagent_app.storage import Database, transaction
from hagent_app.tools.base import Prepared, ToolContext
from hagent_app.tools.executor import ApprovalOutcome

_LOG = logging.getLogger(__name__)

ACTION_EVENT = "mobile_app_notification_action"
ACTION_ID = re.compile(r"HAGENT_(ACCEPT|REJECT)_([0-9a-f]{32})")
REQUEST_ID = re.compile(r"[0-9a-f]{32}")
TOPIC = "approvals"
MAX_LISTED = 10

type Decision = Literal["accept", "reject"]
type Via = Literal["notification", "api"]
type AnswerOutcome = Literal[
    "applied", "late", "not_approver", "no_user", "app_user", "unknown"
]

_COLUMNS = (
    "id, created_at, expires_at, agent_id, agent_version, invocation_id, tool, "
    "summary, request, approvers, targets, problems, status, finished_at, "
    "answered_by, answered_via, reason"
)


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    """One row of ``approval_requests``."""

    id: str
    created_at: str
    expires_at: str
    agent_id: str
    agent_version: int
    invocation_id: str | None
    tool: str
    summary: str
    request: Any
    approvers: list[str]
    targets: list[str]
    problems: list[str]
    status: str
    finished_at: str | None
    answered_by: str | None
    answered_via: str | None
    reason: str | None

    @classmethod
    def from_row(cls, row: Sequence[Any]) -> ApprovalRecord:
        """Build a record from a row in ``_COLUMNS`` order."""
        return cls(
            id=row[0],
            created_at=row[1],
            expires_at=row[2],
            agent_id=row[3],
            agent_version=row[4],
            invocation_id=row[5],
            tool=row[6],
            summary=row[7],
            request=json.loads(row[8]),
            approvers=json.loads(row[9]),
            targets=json.loads(row[10]),
            problems=json.loads(row[11]),
            status=row[12],
            finished_at=row[13],
            answered_by=row[14],
            answered_via=row[15],
            reason=row[16],
        )

    def to_json(self) -> dict[str, Any]:
        """The JSON form for the management API."""
        return {
            "id": self.id,
            "status": self.status,
            "agent_id": self.agent_id,
            "agent_version": self.agent_version,
            "invocation_id": self.invocation_id,
            "tool": self.tool,
            "summary": self.summary,
            "request": self.request,
            "approvers": self.approvers,
            "targets": self.targets,
            "problems": self.problems,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "finished_at": self.finished_at,
            "answered_by": self.answered_by,
            "answered_via": self.answered_via,
            "reason": self.reason,
        }


@dataclass
class _Waiter:
    approvers: frozenset[str]
    future: asyncio.Future[str] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )


def summarize(agent_name: str, tool: str, prepared: Prepared) -> str:
    """The notification text. It uses only checked services and entities.

    The text never contains free-form service data, so a model cannot write
    a misleading prompt to the approver.
    """
    lines = [f"Agent {agent_name!r} asks to run {tool}."]
    for label, values in (
        ("Services", prepared.services),
        ("Entities", prepared.entities),
    ):
        if values:
            names = sorted(values)
            more = len(names) - MAX_LISTED
            text = ", ".join(names[:MAX_LISTED])
            if more > 0:
                text += f" and {more} more"
            lines.append(f"{label}: {text}")
    return "\n".join(lines)


def message_for(request_id: str, summary: str) -> ApprovalMessage:
    """The notification of one request."""
    return ApprovalMessage(
        request_id=request_id,
        tag=f"hagent-approval-{request_id}",
        title="hagent: approval needed",
        message=summary,
        accept_action=f"HAGENT_ACCEPT_{request_id}",
        reject_action=f"HAGENT_REJECT_{request_id}",
    )


class ApprovalService:
    """Approval requests, answers and their records.

    ``approvers`` returns the current approver list. ``app_user`` returns
    the HA user ID of the app's own token, or ``None`` if it is not known
    yet. The service refuses to send a request while it is ``None``.
    """

    def __init__(
        self,
        db: Database,
        notifier: Notifier,
        config: ApprovalsConfig,
        *,
        app_user: Callable[[], Any],
        events: ChangeEvents | None = None,
    ) -> None:
        self._db = db
        self._notifier = notifier
        self._config = config
        self._app_user = app_user
        self._events = events
        self._waiters: dict[str, _Waiter] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closed = False

    def current_approvers(self) -> frozenset[str]:
        """The approvers of the settings, without the app's own HA user."""
        app_user = self._app_user()
        return frozenset(a for a in self._config.approvers if a != app_user)

    # -- the gate

    async def request(
        self, ctx: ToolContext, tool: str, prepared: Prepared
    ) -> ApprovalOutcome:
        """Hold the call until an approver answers or the time runs out."""
        if self._closed:
            return _unavailable("the approval service is not running")
        if self._app_user() is None:
            return _unavailable("the HA user of the app is not known yet")
        approvers = self.current_approvers()
        if not approvers:
            return _unavailable("no approvers are set")

        request_id = secrets.token_hex(16)
        summary = summarize(ctx.agent.name, tool, prepared)
        message = message_for(request_id, summary)
        created = _now()
        expires = created + timedelta(seconds=self._config.timeout_seconds)
        waiter = _Waiter(approvers)
        await self._insert(
            ctx=ctx,
            request_id=request_id,
            tool=tool,
            summary=summary,
            prepared=prepared,
            approvers=approvers,
            created=created,
            expires=expires,
        )
        # Register the waiter before the send, so that a fast answer finds it.
        self._waiters[request_id] = waiter
        self._publish(request_id, "created", "pending")
        delivery = Delivery((), ())
        try:
            delivery = await self._notifier.send(message, sorted(approvers))
            await self._set_delivery(request_id, delivery)
            if not delivery.targets:
                if await self._finish(
                    request_id, "failed", reason="no notification was sent"
                ):
                    return _unavailable("no approval notification reached an approver")
                # An approver answered through the API during the send.
                status = await self._status(request_id)
            else:
                status = await self._wait(request_id, waiter, expires)
        except asyncio.CancelledError:
            await asyncio.shield(
                self._finish(request_id, "cancelled", reason="the agent turn stopped")
            )
            await asyncio.shield(self._notifier.clear(message, delivery.targets))
            raise
        except Exception:
            # Fail closed, also if an answer already decided the row.
            _LOG.exception("approval request %s failed", request_id)
            await self._finish(request_id, "failed", reason="internal error")
            await self._notifier.clear(message, delivery.targets)
            return _unavailable("the approval request failed")
        finally:
            self._waiters.pop(request_id, None)
        await self._notifier.clear(message, delivery.targets)
        return _OUTCOMES.get(status) or _unavailable(
            f"the approval request ended: {status}"
        )

    async def _wait(self, request_id: str, waiter: _Waiter, expires: datetime) -> str:
        # The time limit counts from the creation of the request, so a slow
        # send does not make the agent turn wait longer.
        remaining = max(0.0, (expires - _now()).total_seconds())
        try:
            async with asyncio.timeout(remaining):
                return await asyncio.shield(waiter.future)
        except TimeoutError:
            pass
        if await self._finish(request_id, "timeout", reason="no answer in time"):
            return "timeout"
        # An answer won the race with the timeout. The row holds it.
        return await self._status(request_id)

    async def _status(self, request_id: str) -> str:
        record = await self.get(request_id)
        return record.status if record else "failed"

    # -- answers

    def on_event(self, event: Mapping[str, Any]) -> None:
        """The handler of the action event. It must be fast."""
        if event.get("event_type") != ACTION_EVENT:
            return
        data = event.get("data")
        action = data.get("action") if isinstance(data, Mapping) else None
        if not isinstance(action, str):
            return
        match = ACTION_ID.fullmatch(action)
        if match is None:
            return
        decision: Decision = "accept" if match.group(1) == "ACCEPT" else "reject"
        context = event.get("context")
        user_id = context.get("user_id") if isinstance(context, Mapping) else None
        task = asyncio.create_task(
            self.answer(
                match.group(2),
                decision,
                user_id if isinstance(user_id, str) else None,
                "notification",
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def answer(
        self, request_id: str, decision: Decision, user_id: str | None, via: Via
    ) -> AnswerOutcome:
        """Apply one answer. Return what happened to it."""
        if not REQUEST_ID.fullmatch(request_id):
            return "unknown"
        record = await self.get(request_id)
        if record is None:
            _LOG.debug("answer for an unknown approval request")
            return "unknown"
        outcome: AnswerOutcome
        waiter = self._waiters.get(request_id)
        if user_id is None:
            outcome = "no_user"
        elif user_id == self._app_user():
            outcome = "app_user"
        elif user_id not in record.approvers or user_id not in self.current_approvers():
            outcome = "not_approver"
        elif waiter is None or waiter.future.done():
            outcome = "late"
        elif user_id not in waiter.approvers:
            outcome = "not_approver"
        else:
            status = "accepted" if decision == "accept" else "rejected"
            won = await self._finish(
                request_id, status, answered_by=user_id, answered_via=via
            )
            outcome = "applied" if won else "late"
            if won and not waiter.future.done():
                waiter.future.set_result(status)
        await self._record_answer(request_id, decision, user_id, via, outcome)
        if outcome == "applied":
            _LOG.info("approval request %s: %s by an approver", request_id, decision)
        else:
            _LOG.warning(
                "approval request %s: %s answer via %s had no effect (%s)",
                request_id,
                decision,
                via,
                outcome,
            )
        return outcome

    # -- life cycle

    async def expire_pending(self, reason: str) -> int:
        """Mark every pending row as expired. Return the count."""
        now = _now().isoformat()

        def run(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "UPDATE approval_requests SET status = 'expired', finished_at = ?, "
                "reason = ? WHERE status = 'pending'",
                (now, reason),
            )
            return cur.rowcount

        return await self._db.run(run)

    async def close(self) -> None:
        """Stop: expire pending rows and wake every waiter."""
        self._closed = True
        for request_id in list(self._waiters):
            await self._finish(request_id, "expired", reason="the app stopped")
        for waiter in self._waiters.values():
            if not waiter.future.done():
                waiter.future.set_result("expired")
        for task in list(self._tasks):
            task.cancel()

    # -- reads

    async def get(self, request_id: str) -> ApprovalRecord | None:
        """One request, or ``None``."""

        def run(conn: sqlite3.Connection) -> ApprovalRecord | None:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM approval_requests WHERE id = ?",  # noqa: S608
                (request_id,),
            ).fetchone()
            return ApprovalRecord.from_row(row) if row else None

        return await self._db.run(run)

    async def list_requests(
        self, *, status: str | None = None, limit: int = 100
    ) -> list[ApprovalRecord]:
        """The newest requests first, optionally of one status."""

        def run(conn: sqlite3.Connection) -> list[ApprovalRecord]:
            sql = f"SELECT {_COLUMNS} FROM approval_requests"  # noqa: S608
            params: list[Any] = []
            if status is not None:
                sql += " WHERE status = ?"
                params.append(status)
            sql += " ORDER BY created_at DESC, id LIMIT ?"
            params.append(limit)
            return [ApprovalRecord.from_row(r) for r in conn.execute(sql, params)]

        return await self._db.run(run)

    async def answers(self, request_id: str) -> list[dict[str, Any]]:
        """Every recorded answer of one request, oldest first."""

        def run(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                "SELECT received_at, decision, user_id, via, outcome "
                "FROM approval_answers WHERE request_id = ? ORDER BY id",
                (request_id,),
            )
            keys = ("received_at", "decision", "user_id", "via", "outcome")
            return [dict(zip(keys, r, strict=True)) for r in rows]

        return await self._db.run(run)

    # -- writes

    async def _insert(  # noqa: PLR0913
        self,
        *,
        ctx: ToolContext,
        request_id: str,
        tool: str,
        summary: str,
        prepared: Prepared,
        approvers: frozenset[str],
        created: datetime,
        expires: datetime,
    ) -> None:
        values = (
            request_id,
            created.isoformat(),
            expires.isoformat(),
            ctx.agent.id,
            ctx.agent.version,
            ctx.invocation_id,
            tool,
            summary,
            json.dumps(dict(prepared.request), default=str),
            json.dumps(sorted(approvers)),
        )

        def run(conn: sqlite3.Connection) -> None:
            with transaction(conn):
                conn.execute(
                    "INSERT INTO approval_requests (id, created_at, expires_at, "
                    "agent_id, agent_version, invocation_id, tool, summary, request, "
                    "approvers, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
                    values,
                )

        await self._db.run(run)

    async def _set_delivery(self, request_id: str, delivery: Delivery) -> None:
        targets = json.dumps(list(delivery.targets))
        problems = json.dumps(list(delivery.problems))

        def run(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE approval_requests SET targets = ?, problems = ? WHERE id = ?",
                (targets, problems, request_id),
            )

        await self._db.run(run)

    async def _finish(
        self,
        request_id: str,
        status: str,
        *,
        reason: str | None = None,
        answered_by: str | None = None,
        answered_via: str | None = None,
    ) -> bool:
        """Move a pending row to ``status``. Return False if it was not pending."""
        now = _now().isoformat()

        def run(conn: sqlite3.Connection) -> bool:
            cur = conn.execute(
                "UPDATE approval_requests SET status = ?, finished_at = ?, reason = ?, "
                "answered_by = ?, answered_via = ? WHERE id = ? AND status = 'pending'",
                (status, now, reason, answered_by, answered_via, request_id),
            )
            return cur.rowcount == 1

        won = await self._db.run(run)
        if won:
            self._publish(request_id, "updated", status)
        return won

    async def _record_answer(
        self,
        request_id: str,
        decision: Decision,
        user_id: str | None,
        via: Via,
        outcome: AnswerOutcome,
    ) -> None:
        now = _now().isoformat()

        def run(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO approval_answers (request_id, received_at, decision, "
                "user_id, via, outcome) VALUES (?, ?, ?, ?, ?, ?)",
                (request_id, now, decision, user_id, via, outcome),
            )

        try:
            await self._db.run(run)
        except sqlite3.Error:
            _LOG.exception("cannot record an approval answer")

    def _publish(self, request_id: str, action: str, status: str) -> None:
        if self._events is not None:
            self._events.publish(TOPIC, action, request_id, {"status": status})


_OUTCOMES = {
    "accepted": ApprovalOutcome(True, "approved", "an approver accepted the call"),
    "rejected": ApprovalOutcome(
        False, "approval_rejected", "the approver rejected the call"
    ),
    "timeout": ApprovalOutcome(
        False, "approval_timeout", "no approver answered in time"
    ),
}


def _unavailable(message: str) -> ApprovalOutcome:
    return ApprovalOutcome(False, "approval_unavailable", message)
