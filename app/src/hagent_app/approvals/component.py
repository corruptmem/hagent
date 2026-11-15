"""The approvals component: the approval gate, the answer listener and routes.

Plug it in, in ``default_components()``, before the tools component
starts::

    approvals = ApprovalsComponent(http.events)
    http.add_routes(approvals.routes())
    tools.approval = approvals.gate

``approvals.notifier`` replaces the app-side notifier, for example with a
sender in HA Core (task-17). Set it before the app starts.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

from aiohttp import web

from hagent_app.approvals.api import ApprovalsApi
from hagent_app.approvals.config import ApprovalsConfig
from hagent_app.approvals.notifier import MobileAppNotifier, Notifier
from hagent_app.approvals.service import ACTION_EVENT, ApprovalService
from hagent_app.ha.client import HAError, Subscription
from hagent_app.http.events import ChangeEvents
from hagent_app.tools.base import Prepared, ToolContext
from hagent_app.tools.executor import ApprovalOutcome

if TYPE_CHECKING:
    from hagent_app.app import AppContext

_LOG = logging.getLogger(__name__)


class ApprovalsUnavailableError(RuntimeError):
    """The approvals component has not started."""


class _Gate:
    """The ``ApprovalGate`` for the tool executor. It fails closed."""

    def __init__(self, component: ApprovalsComponent) -> None:
        self._component = component

    async def request(
        self, ctx: ToolContext, tool: str, prepared: Prepared
    ) -> ApprovalOutcome:
        service = self._component.service_or_none()
        if service is None:
            return ApprovalOutcome(
                False, "approval_unavailable", "the approvals component is not running"
            )
        return await service.request(ctx, tool, prepared)


class ApprovalsComponent:
    """Owns the approval service. See the module text."""

    name = "approvals"

    def __init__(self, events: ChangeEvents | None = None) -> None:
        self._events = events
        self.gate = _Gate(self)
        self.notifier: Notifier | None = None
        self._service: ApprovalService | None = None
        self._ctx: AppContext | None = None
        self._sub: Subscription | None = None
        self._app_user: str | None = None
        self._user_task: asyncio.Task[None] | None = None
        self._api = ApprovalsApi(self.service)

    def routes(self) -> list[web.RouteDef]:
        """The routes for ``HttpServer.add_routes``."""
        return self._api.routes()

    def service_or_none(self) -> ApprovalService | None:
        """The running service, or ``None``."""
        return self._service

    def service(self) -> ApprovalService:
        """The running service. Raise before start."""
        if self._service is None:
            msg = "the approvals component is not running"
            raise ApprovalsUnavailableError(msg)
        return self._service

    async def start(self, ctx: AppContext) -> None:
        """Expire old requests, then listen for answers."""
        config = ctx.config.section("approvals", ApprovalsConfig)
        notifier = self.notifier or MobileAppNotifier(
            ctx.ha, ctx.states, config.notify_services
        )
        service = ApprovalService(
            ctx.db,
            notifier,
            config,
            app_user=lambda: self._app_user,
            events=self._events,
        )
        expired = await service.expire_pending("the app restarted")
        if expired:
            _LOG.warning(
                "expired %d approval requests from before the restart", expired
            )
        self._ctx = ctx
        self._sub = await ctx.ha.subscribe_events(service.on_event, ACTION_EVENT)
        ctx.ha.add_connect_listener(self._on_connect)
        if ctx.ha.connected:
            self._user_task = asyncio.create_task(self._on_connect())
        self._service = service

    async def stop(self) -> None:
        """Stop listening and expire every pending request."""
        service, self._service = self._service, None
        ctx, self._ctx = self._ctx, None
        if self._user_task is not None:
            self._user_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._user_task
            self._user_task = None
        if ctx is not None and self._sub is not None:
            await ctx.ha.unsubscribe(self._sub)
        self._sub = None
        if service is not None:
            await service.close()

    async def _on_connect(self) -> None:
        """Read the HA user of the app's token. Answers from it never count."""
        ctx = self._ctx
        if ctx is None:
            return
        try:
            user: Any = await ctx.ha.command({"type": "auth/current_user"})
        except (HAError, TimeoutError) as err:
            _LOG.warning("cannot read the HA user of the app: %s", err)
            return
        user_id = user.get("id") if isinstance(user, dict) else None
        if isinstance(user_id, str) and user_id:
            self._app_user = user_id
        else:
            _LOG.warning("HA returned no user ID for the app's token")
