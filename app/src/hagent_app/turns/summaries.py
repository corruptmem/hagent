"""Running summaries per source and per Assist conversation (design 13.1).

The prompt of a turn holds the running summary of its source plus the
latest records, not a full transcript. After each turn, ``SummaryWriter``
checks the records that the summary does not cover. If there are more than
``recent_records + summary_batch``, the summary model of the definition
folds the older records into the summary. The latest ``recent_records``
records stay in full.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from pydantic_ai import Agent as ModelAgent
from pydantic_ai.models import Model
from pydantic_ai.usage import UsageLimits

from hagent_app.agents.definition import ModelRef
from hagent_app.agents.models import Agent
from hagent_app.turns.config import TurnsConfig
from hagent_app.turns.history import TurnHistory
from hagent_app.turns.prompt import clip, record_text

_LOG = logging.getLogger(__name__)

type ModelFactory = Callable[[ModelRef, str], Model]

SUMMARY_INSTRUCTIONS = """\
You keep the running summary of the invocations of one source of a home \
automation agent. You receive the current summary and older invocation \
records. Write one new summary that replaces the current summary. Keep the \
facts that later invocations can need: patterns, decisions, results, errors \
and open items. Drop repeated detail. Write plain text, at most {limit} \
characters. Return only the summary."""

# Each record in the summary request can be longer than in the turn prompt.
_RECORD_FACTOR = 2


class SummaryWriter:
    """Update the running summary of one source of an agent."""

    def __init__(
        self, history: TurnHistory, model_for: ModelFactory, config: TurnsConfig
    ) -> None:
        self._history = history
        self._model_for = model_for
        self._config = config
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def update(self, agent: Agent, source_key: str, turn_id: str) -> bool:
        """Fold older records into the summary if enough are waiting.

        ``turn_id`` is the turn that triggered the update. The compaction
        record belongs to it. Return True if the summary changed.
        """
        lock = self._locks.setdefault((agent.id, source_key), asyncio.Lock())
        async with lock:
            current = await self._history.summary(agent.id, source_key)
            records = await self._history.unsummarized(
                agent.id, source_key, current.covers_until if current else None
            )
            # A turn that still runs (parallel mode) finishes with an earlier
            # start time. Fold only turns before it, so that the summary
            # never skips it.
            running = await self._history.earliest_running(agent.id, source_key)
            if running is not None:
                records = [r for r in records if r.started_at < running]
            keep = self._config.recent_records
            if len(records) <= keep + self._config.summary_batch:
                return False
            fold = records[: len(records) - keep]
            ref = agent.definition.models.summary_model
            limit = self._config.summary_max_chars
            model_agent = ModelAgent(
                self._model_for(ref, agent.id),
                instructions=SUMMARY_INSTRUCTIONS.format(limit=limit),
                output_type=str,
            )
            record_limit = self._config.record_max_chars * _RECORD_FACTOR
            prompt = (
                "Current summary:\n"
                + (current.summary if current else "(none)")
                + "\n\nOlder records, oldest first:\n"
                + "\n".join("- " + record_text(r, record_limit) for r in fold)
            )
            run = await model_agent.run(
                prompt, usage_limits=UsageLimits(request_limit=2)
            )
            summary = clip(run.output.strip(), limit)
            count = (current.turn_count if current else 0) + len(fold)
            await self._history.set_summary(
                agent.id, source_key, summary, fold[-1].started_at, count
            )
            await self._history.add_compaction(
                turn_id,
                "running_summary",
                {
                    "source_key": source_key,
                    "turns": [r.id for r in fold],
                    "model": ref.model_dump(),
                    "previous_summary": current.summary if current else None,
                },
                summary=summary,
            )
            return True
