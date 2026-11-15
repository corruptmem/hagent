"""The turns component: the turn runner and the turn history."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pydantic_ai.models import Model

from hagent_app.agents.definition import ModelRef
from hagent_app.agents.models import Agent
from hagent_app.tools import ToolsComponent
from hagent_app.turns.config import TurnsConfig
from hagent_app.turns.history import TurnHistory
from hagent_app.turns.models import TurnInput, TurnResult
from hagent_app.turns.prompt import KnowledgeContextSource
from hagent_app.turns.runner import TurnRunner
from hagent_app.turns.summaries import ModelFactory

if TYPE_CHECKING:
    from hagent_app.app import AppContext
    from hagent_app.providers import ProvidersComponent


_LOG = logging.getLogger(__name__)


class TurnsUnavailableError(RuntimeError):
    """The turns component has not started."""


class TurnsComponent:
    """Owns the turn runner and the turn history.

    The constructor plugs the history into ``ToolsComponent.history``, so
    that ``memory.search`` reads it. Create this component before the app
    starts, and list it after ``tools`` in ``default_components``.

    The router (task-12) calls ``run_turn``. The Inspect screens (task-19)
    read ``history``. ``default_components`` sets ``history.index`` and
    ``knowledge_context`` from the knowledge component (task-11).
    """

    name = "turns"

    def __init__(
        self,
        providers: ProvidersComponent,
        tools: ToolsComponent,
        *,
        model_for: ModelFactory | None = None,
    ) -> None:
        self._providers = providers
        self._tools = tools
        self._model_for = model_for
        self.history = TurnHistory()
        tools.history = self.history
        self.knowledge_context: KnowledgeContextSource | None = None
        self._runner: TurnRunner | None = None

    def _chat_model(self, ref: ModelRef, agent_id: str) -> Model:
        return self._providers.chat_model(ref.provider, ref.model, agent_id=agent_id)

    @property
    def runner(self) -> TurnRunner:
        """The running turn runner. Raise before start."""
        if self._runner is None:
            msg = "the turns component is not running"
            raise TurnsUnavailableError(msg)
        return self._runner

    async def run_turn(self, agent: Agent, turn: TurnInput) -> TurnResult:
        """Run one agent turn. See ``TurnRunner.run_turn``."""
        return await self.runner.run_turn(agent, turn)

    async def start(self, ctx: AppContext) -> None:
        """Bind the history to the database and build the runner."""
        config = ctx.config.section("turns", TurnsConfig)
        self.history.bind(ctx.db)
        failed = await self.history.fail_running()
        if failed:
            _LOG.info("marked %d turns from the last run as failed", failed)
        self._runner = TurnRunner(
            self._tools.executor,
            self.history,
            self._model_for or self._chat_model,
            config,
            self.knowledge_context,
        )

    async def stop(self) -> None:
        """Stop the background work and release the database."""
        if self._runner is not None:
            await self._runner.close()
            self._runner = None
        self.history.bind(None)
