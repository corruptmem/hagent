"""The approvals component: the approval gate, the answer listener and routes.

Plug it in, in ``default_components()``, before the tools component
starts::

    approvals = ApprovalsComponent(http.events)
    http.add_routes(approvals.routes())
    tools.approval = approvals.gate

``approvals.notifier`` replaces the app-side notifier, for example with a
sender in HA Core (task-17). Set it before the app starts.

``settings`` (task-20) supplies the approvers, the notify-service
overrides and the time limit from the management UI. When the user set no
approval settings, the component reads the ``approvals`` option section.
A change in the settings takes effect for the next request.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from aiohttp import web

from hagent_app.agents import AgentReader
from hagent_app.approvals.api import ApprovalsApi
from hagent_app.approvals.config import ApprovalsConfig
from hagent_app.approvals.notifier import MobileAppNotifier, Notifier
from hagent_app.approvals.service import ACTION_EVENT, ApprovalService
from hagent_app.ha.client import HAError, Subscription
from hagent_app.http.events import ChangeEvent, ChangeEvents
from hagent_app.tools.base import Prepared, ToolContext
from hagent_app.tools.executor import ApprovalOutcome

if TYPE_CHECKING:
    from hagent_app.app import AppContext
    from hagent_app.settings import SettingsComponent

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

    def __init__(
        self,
        events: ChangeEvents | None = None,
        settings: SettingsComponent | None = None,
    ) -> None:
        self._events = events
        self._settings = settings
        self._options: ApprovalsConfig | None = None
        self._unsubscribe_settings: Callable[[], None] | None = None
        self.gate = _Gate(self)
        self.notifier: Notifier | None = None
        self._service: ApprovalService | None = None
        self._ctx: AppContext | None = None
        self._sub: Subscription | None = None
        self._app_user: str | None = None
        self._user_task: asyncio.Task[None] | None = None
        self._trust_any_peer = False
        self._unsubscribe_agents: Callable[[], None] | None = None
        self._api = ApprovalsApi(self.service, lambda: self._trust_any_peer)

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
        self._options = ctx.config.section("approvals", ApprovalsConfig)
        config = self._effective()
        notifier = self.notifier or MobileAppNotifier(
            ctx.ha, ctx.states, config.notify_services
        )
        service = ApprovalService(
            ctx.db,
            notifier,
            config,
            app_user=lambda: self._app_user,
            agents=AgentReader(ctx.db),
            events=self._events,
        )
        self._trust_any_peer = config.dev_trust_user_header
        if config.dev_trust_user_header:
            _LOG.warning(
                "approvals.dev_trust_user_header is on: the approvals API "
                "trusts X-Remote-User-Id from every allowed address. Use it "
                "only for local development."
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
        if self._events is not None:
            self._unsubscribe_agents = self._events.subscribe(self._on_change)
        if self._settings is not None:
            self._unsubscribe_settings = self._settings.subscribe(self._on_settings)
        self._service = service

    def _effective(self) -> ApprovalsConfig:
        """The approval settings, or else the option section."""
        options = self._options or ApprovalsConfig()
        stored = self._settings.approvals() if self._settings is not None else None
        if stored is None:
            return options
        return ApprovalsConfig(
            approvers=list(stored.approvers),
            timeout_seconds=stored.timeout_seconds,
            notify_services={k: list(v) for k, v in stored.notify_services.items()},
            dev_trust_user_header=options.dev_trust_user_header,
        )

    def _on_settings(self, names: frozenset[str]) -> None:
        """Apply a change of the approval settings."""
        service = self._service
        if service is None or "approvals" not in names:
            return
        config = self._effective()
        service.set_config(config)
        if isinstance(service.notifier, MobileAppNotifier):
            service.notifier.set_overrides(config.notify_services)

    def _on_change(self, event: ChangeEvent) -> None:
        """End the pending requests of an agent that changed (review M1)."""
        service = self._service
        if service is not None and event.topic == "agents":
            service.on_agent_change(event.id)

    async def stop(self) -> None:
        """Stop listening and expire every pending request."""
        service, self._service = self._service, None
        if self._unsubscribe_agents is not None:
            self._unsubscribe_agents()
            self._unsubscribe_agents = None
        if self._unsubscribe_settings is not None:
            self._unsubscribe_settings()
            self._unsubscribe_settings = None
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
