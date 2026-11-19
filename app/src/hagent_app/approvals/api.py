"""The management API routes for approvals. ``docs/api.md`` describes them.

The answer route takes the caller from the ``X-Remote-User-Id`` header.
The Supervisor sets this header on each Ingress request to the ID of the HA
user who is logged in, and it removes a copy that the client sends. The API
trusts the header only when the TCP peer is the Supervisor address
(``SUPERVISOR_ADDRESS``), whatever ``IngressGuard`` allows. The
``approvals.dev_trust_user_header`` option turns this check off for local
development.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from aiohttp import web

from hagent_app.http.errors import ApiError, read_json

if TYPE_CHECKING:
    from hagent_app.approvals.service import ApprovalService, Decision

USER_HEADER = "X-Remote-User-Id"
SUPERVISOR_ADDRESS = "172.30.32.2"
STATUSES = frozenset(
    {"pending", "accepted", "rejected", "timeout", "failed", "cancelled", "expired"}
)
_ID = "{request_id:[a-f0-9]{32}}"
MAX_LIMIT = 500

_ANSWER_ERRORS = {
    "no_user": (403, "no_user", "the request has no HA user"),
    "app_user": (403, "not_approver", "this HA user cannot approve"),
    "not_approver": (403, "not_approver", "this HA user is not an approver"),
    "late": (409, "approval_closed", "the approval request is not pending"),
    "unknown": (404, "approval_not_found", "no approval request with this ID"),
}


class ApprovalsApi:
    """The routes of the approvals area."""

    def __init__(
        self,
        service: Callable[[], ApprovalService],
        trust_any_peer: Callable[[], bool] = lambda: False,
    ) -> None:
        self._service = service
        self._trust_any_peer = trust_any_peer

    def routes(self) -> list[web.RouteDef]:
        """The route table."""
        return [
            web.get("/api/approvals", self.index),
            web.get(f"/api/approvals/{_ID}", self.get),
            web.post(f"/api/approvals/{_ID}/answer", self.answer),
        ]

    def _user(self, request: web.Request) -> str | None:
        """The HA user of the request, or ``None`` if the app cannot trust it."""
        if request.remote != SUPERVISOR_ADDRESS and not self._trust_any_peer():
            return None
        value = request.headers.get(USER_HEADER, "").strip()
        return value or None

    def _running(self) -> ApprovalService:
        try:
            return self._service()
        except RuntimeError as err:
            raise ApiError(503, "not_ready", str(err)) from err

    async def index(self, request: web.Request) -> web.Response:
        """GET /api/approvals: the newest requests first."""
        service = self._running()
        status = request.query.get("status")
        if status is not None and status not in STATUSES:
            raise ApiError(400, "invalid_query", f"unknown status {status!r}")
        try:
            limit = int(request.query.get("limit", "100"))
        except ValueError as err:
            raise ApiError(400, "invalid_query", "limit must be an integer") from err
        if not 1 <= limit <= MAX_LIMIT:
            raise ApiError(400, "invalid_query", f"limit must be 1 to {MAX_LIMIT}")
        user = self._user(request)
        approvers = service.current_approvers()
        records = await service.list_requests(status=status, limit=limit)
        return web.json_response(
            {
                "approvals": [r.to_json() for r in records],
                "can_answer": user is not None and user in approvers,
            }
        )

    async def get(self, request: web.Request) -> web.Response:
        """GET /api/approvals/{id}: one request and its recorded answers."""
        service = self._running()
        request_id = request.match_info["request_id"]
        record = await service.get(request_id)
        if record is None:
            raise ApiError(
                404, "approval_not_found", "no approval request with this ID"
            )
        body: dict[str, Any] = record.to_json()
        body["answers"] = await service.answers(request_id)
        user = self._user(request)
        body["can_answer"] = (
            record.status == "pending"
            and user is not None
            and user in record.approvers
            and user in service.current_approvers()
        )
        return web.json_response(body)

    async def answer(self, request: web.Request) -> web.Response:
        """POST /api/approvals/{id}/answer: accept or reject."""
        service = self._running()
        body = await read_json(request)
        decision = body.get("decision") if isinstance(body, dict) else None
        if decision not in ("accept", "reject"):
            raise ApiError(400, "invalid_body", "decision must be 'accept' or 'reject'")
        request_id = request.match_info["request_id"]
        outcome = await service.answer(
            request_id, cast("Decision", decision), self._user(request), "api"
        )
        if outcome != "applied":
            status, code, message = _ANSWER_ERRORS[outcome]
            raise ApiError(status, code, message)
        record = await service.get(request_id)
        return web.json_response(record.to_json() if record else {})
