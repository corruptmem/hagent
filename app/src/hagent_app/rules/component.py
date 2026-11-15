"""The rules component: the rule store, the rule engine and the HA clock."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from hagent_app.router.retry import RetryPolicy
from hagent_app.rules.config import RulesConfig
from hagent_app.rules.stage import RuleEngine
from hagent_app.rules.store import AgentStateStores, RuleStore
from hagent_app.rules.templates import TemplateClock

if TYPE_CHECKING:
    from hagent_app.app import AppContext
    from hagent_app.providers import ProvidersComponent
    from hagent_app.router.rules import RuleOutcome, RuleRequest
    from hagent_app.tools import ToolsComponent

_LOG = logging.getLogger(__name__)


class RulesUnavailableError(RuntimeError):
    """The rules component has not started."""


class RulesComponent:
    """Owns the rule store and the rule engine (design section 10).

    The constructor plugs the store into ``tools.agent_state`` (kind
    ``rule``). ``default_components`` also sets ``router.rules`` to this
    component and ``turns.history.rules`` to ``store``. List it after
    ``tools`` and before ``router``: the router runs queued invocations as
    soon as it starts.
    """

    name = "rules"

    def __init__(self, tools: ToolsComponent, providers: ProvidersComponent) -> None:
        self._tools = tools
        self._providers = providers
        self.store = RuleStore()
        self.agent_state = AgentStateStores({"rule": self.store})
        tools.agent_state = self.agent_state
        self._engine: RuleEngine | None = None
        self._clock = TemplateClock()
        self._ctx: AppContext | None = None

    @property
    def engine(self) -> RuleEngine:
        """The running rule engine. Raise before start."""
        if self._engine is None:
            msg = "the rules component is not running"
            raise RulesUnavailableError(msg)
        return self._engine

    async def try_rules(self, request: RuleRequest) -> RuleOutcome:
        """The ``RuleStage`` of the router."""
        return await self.engine.try_rules(request)

    def clock(self) -> TemplateClock:
        """The clock of the templates, with HA's time zone when known."""
        return self._clock

    async def start(self, ctx: AppContext) -> None:
        """Bind the store and build the engine."""
        config = ctx.config.section("rules", RulesConfig)
        self._ctx = ctx
        self.store.bind(ctx.db)
        self._engine = RuleEngine(
            self.store,
            ctx.db,
            self._tools.executor,
            lambda: self._providers.decisions,
            clock=self.clock,
            retry=RetryPolicy(
                attempts=config.decision_retry_attempts,
                base_delay=config.decision_retry_base_delay,
                max_delay=config.decision_retry_max_delay,
            ),
            render_seconds=config.render_seconds,
            cache_entries=config.cache_entries,
        )
        ctx.ha.add_connect_listener(self._load_time_zone)
        if ctx.ha.connected:
            await self._load_time_zone()

    async def _load_time_zone(self) -> None:
        """Read HA's time zone for ``now()``. Keep the last one on failure."""
        ctx = self._ctx
        if ctx is None:
            return
        try:
            config = await ctx.ha.command({"type": "get_config"})
            name = config.get("time_zone") if isinstance(config, dict) else None
            if isinstance(name, str):
                self._clock = TemplateClock(ZoneInfo(name))
        except ZoneInfoNotFoundError, ValueError:
            _LOG.warning("HA reports an unknown time zone")
        except Exception:
            _LOG.warning("cannot read the HA time zone", exc_info=True)

    async def stop(self) -> None:
        """Release the database."""
        self._engine = None
        self.store.bind(None)
        self._ctx = None
