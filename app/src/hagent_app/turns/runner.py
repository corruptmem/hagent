"""The agent turn: one Pydantic AI run with hagent tools (design 9.5).

``TurnRunner.run_turn(agent, turn)`` is the single entry point for the
router (task-12). It runs these steps:

1. Build the prompt in the order of design section 9.5 (``prompt.py``).
2. Record the turn in the history with status ``running``.
3. Give the model the tools of ``ToolExecutor.tools_for``. Each tool call
   goes to ``ToolExecutor.execute``, which checks the scope. History keeps
   the full result. The model receives at most ``tool_result_max_chars``.
4. Run the Pydantic AI agent with the per-turn limits of the definition:
   maximum tool calls and maximum tokens. ``Caching`` adds provider cache
   markers. ``TieredCompaction`` clears old tool results inside the turn
   when the context is larger than ``compaction_target_tokens``.
5. Validate a structured result against its JSON schema. On a failure, the
   model gets one correction attempt.
6. Record the result, the messages, the usage and each compaction. Start
   the running summary update in the background.

The runner never raises for a model, provider or limit failure. It returns
a ``TurnResult`` with ``status`` and ``stop_reason``, and the router returns
the fallback (design section 9.7). A cancelled turn records ``cancelled``
and raises ``CancelledError`` again.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic_ai import Agent as ModelAgent
from pydantic_ai import ModelRetry, RunContext, StructuredDict, capture_run_messages
from pydantic_ai import Tool as ModelTool
from pydantic_ai.capabilities import AbstractCapability, Caching
from pydantic_ai.exceptions import (
    ModelAPIError,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
)
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelResponse,
    ToolReturnPart,
)
from pydantic_ai.usage import UsageLimits
from pydantic_ai_harness.compaction import ClearToolResults, TieredCompaction
from pydantic_ai_harness.warn_on_cache_busts import WarnOnCacheBusts

from hagent_app.agents.models import Agent
from hagent_app.providers.errors import BudgetExceededError, ProviderError
from hagent_app.tools import ToolContext, ToolExecutor
from hagent_app.tools.base import ToolSpec
from hagent_app.turns.config import TurnsConfig
from hagent_app.turns.history import TurnHistory
from hagent_app.turns.models import (
    StopReason,
    TurnInput,
    TurnResult,
    TurnStatus,
    TurnUsage,
)
from hagent_app.turns.prompt import KnowledgeContextSource, Prompt, PromptBuilder
from hagent_app.turns.summaries import ModelFactory, SummaryWriter

_LOG = logging.getLogger(__name__)

PLACEHOLDER = "[tool result cleared]"
# Provider tool names allow letters, digits, "_" and "-", but not ".".
_NAME_SEPARATOR = "__"


def model_tool_name(name: str) -> str:
    """The provider-safe name of a tool: ``ha.get_state`` -> ``ha__get_state``."""
    return name.replace(".", _NAME_SEPARATOR)


@dataclass(slots=True)
class _TurnState:
    """Mutable state of one running turn."""

    turn_id: str
    tool_calls: int = 0
    invalid_result: bool = False


class TurnRunner:
    """Run agent turns. One instance serves every agent."""

    def __init__(
        self,
        executor: Callable[[], ToolExecutor],
        history: TurnHistory,
        model_for: ModelFactory,
        config: TurnsConfig | None = None,
        knowledge_context: KnowledgeContextSource | None = None,
    ) -> None:
        self._executor = executor
        self._history = history
        self._model_for = model_for
        self._knowledge_context = knowledge_context
        self.config = config or TurnsConfig()
        self.summaries = SummaryWriter(history, model_for, self.config)
        self._background: set[asyncio.Task[None]] = set()

    async def run_turn(self, agent: Agent, turn: TurnInput) -> TurnResult:
        """Run one agent turn for the agent snapshot and the invocation."""
        executor = self._executor()
        state = _TurnState(turn_id=uuid.uuid4().hex)
        ctx = ToolContext(
            agent,
            invocation_id=turn.invocation_id or state.turn_id,
            dry_run=turn.dry_run,
        )
        now = turn.now or datetime.now().astimezone()
        prompt = await PromptBuilder(
            executor, self._history, self.config, self._knowledge_context
        ).build(ctx, turn, now)
        await self._history.begin(
            turn_id=state.turn_id,
            agent_id=agent.id,
            agent_version=agent.version,
            invocation_id=turn.invocation_id,
            source=turn.source,
            source_key=turn.source_key,
            conversation_id=turn.conversation_id,
            dry_run=turn.dry_run,
            input_data=prompt.input_data,
            instructions_sha256=hashlib.sha256(
                prompt.instructions.encode()
            ).hexdigest(),
            prompt=prompt.user_prompt,
        )
        for request in prompt.main_file_requests:
            await self._history.add_compaction(
                state.turn_id,
                "main_file_request",
                {
                    "store_id": request.store_id,
                    "store_name": request.store_name,
                    "chars": request.chars,
                    "limit": request.limit,
                },
            )

        messages: list[ModelMessage] = []
        try:
            with capture_run_messages() as messages:
                result = await self._run(
                    agent, turn, ctx=ctx, prompt=prompt, executor=executor, state=state
                )
        except asyncio.CancelledError:
            await asyncio.shield(
                self._finish(
                    state,
                    messages,
                    status="cancelled",
                    stop_reason="cancelled",
                    error="the invocation was cancelled",
                )
            )
            raise
        except UsageLimitExceeded as err:
            result = await self._finish(
                state,
                messages,
                status="stopped",
                stop_reason=_limit_reason(err),
                error=str(err),
            )
        except BudgetExceededError as err:
            result = await self._finish(
                state, messages, status="failed", stop_reason="budget", error=str(err)
            )
        except (ProviderError, ModelAPIError) as err:
            result = await self._finish(
                state,
                messages,
                status="failed",
                stop_reason="provider_error",
                error=str(err),
            )
        except UnexpectedModelBehavior as err:
            reason: StopReason = "invalid_result" if state.invalid_result else "error"
            result = await self._finish(
                state, messages, status="failed", stop_reason=reason, error=str(err)
            )
        except Exception as err:
            _LOG.exception("agent turn %s failed", state.turn_id)
            result = await self._finish(
                state,
                messages,
                status="failed",
                stop_reason="error",
                error=f"{type(err).__name__}: {err}",
            )
        else:
            result = await self._finish(state, messages, status="ok", output=result)

        if not turn.dry_run and result.status != "cancelled":
            self._spawn(self._after_turn(agent, turn.source_key, state.turn_id))
        return result

    async def _run(  # noqa: PLR0913
        self,
        agent: Agent,
        turn: TurnInput,
        *,
        ctx: ToolContext,
        prompt: Prompt,
        executor: ToolExecutor,
        state: _TurnState,
    ) -> Any:
        definition = agent.definition
        schema: Mapping[str, Any] | None = turn.result_schema
        if schema is None and definition.result.type == "structured":
            schema = definition.result.schema_
        output_type: Any = str
        validator: Draft202012Validator | None = None
        if schema is not None:
            try:
                Draft202012Validator.check_schema(dict(schema))
            except SchemaError as err:
                state.invalid_result = True
                msg = f"the result schema is not valid: {err.message}"
                raise UnexpectedModelBehavior(msg) from err
            validator = Draft202012Validator(dict(schema))
            output_type = StructuredDict(dict(schema), name="result")

        tools = [
            self._model_tool(spec, ctx, executor, state)
            for spec in executor.tools_for(definition)
        ]
        model_agent: ModelAgent[None, Any] = ModelAgent(
            self._model_for(definition.models.chat, agent.id),
            instructions=prompt.instructions,
            tools=tools,
            output_type=output_type,
            # One correction attempt for a result that fails validation.
            retries={"tools": 1, "output": 1},
            capabilities=self._capabilities(),
        )
        if validator is not None:
            check = validator

            @model_agent.output_validator
            async def _validate(output: Any) -> Any:
                errors = sorted(check.iter_errors(output), key=lambda e: list(e.path))
                if errors:
                    state.invalid_result = True
                    details = "; ".join(
                        f"{'/'.join(str(p) for p in e.path) or 'result'}: {e.message}"
                        for e in errors[:5]
                    )
                    msg = f"The result does not match the result schema: {details}"
                    raise ModelRetry(msg)
                state.invalid_result = False
                return output

        run = await model_agent.run(
            prompt.user_prompt,
            usage_limits=UsageLimits(
                request_limit=None,
                tool_calls_limit=definition.budget.max_tool_calls,
                total_tokens_limit=definition.budget.max_tokens,
            ),
        )
        if definition.result.type == "none" and turn.result_schema is None:
            return None
        return run.output

    def _capabilities(self) -> list[AbstractCapability[None]]:
        config = self.config
        return [
            Caching(),
            TieredCompaction(
                tiers=[
                    ClearToolResults(
                        max_tokens=1,
                        keep_pairs=config.compaction_keep_pairs,
                        min_clear_tokens=config.compaction_min_clear_tokens,
                        placeholder=PLACEHOLDER,
                    )
                ],
                target_tokens=config.compaction_target_tokens,
            ),
            WarnOnCacheBusts(),
        ]

    def _model_tool(
        self,
        spec: ToolSpec,
        ctx: ToolContext,
        executor: ToolExecutor,
        state: _TurnState,
    ) -> ModelTool[None]:
        history = self._history
        limit = self.config.tool_result_max_chars

        async def call(run_ctx: RunContext[None], **arguments: Any) -> str:
            result = await executor.execute(ctx, spec.name, arguments)
            state.tool_calls += 1
            text = json.dumps(result.to_data(), ensure_ascii=False, default=str)
            cut = len(text) > limit
            await history.add_tool_call(
                state.turn_id,
                tool_call_id=run_ctx.tool_call_id,
                tool=spec.name,
                arguments=arguments,
                ok=result.ok,
                error_code=result.error_code,
                result_text=text,
                cut=cut,
                audit_id=result.audit_id,
            )
            if not cut:
                return text
            await history.add_compaction(
                state.turn_id,
                "tool_result_cut",
                {
                    "tool": spec.name,
                    "tool_call_id": run_ctx.tool_call_id,
                    "chars": len(text),
                    "kept_chars": limit,
                },
            )
            return (
                text[:limit] + f" [cut: the result has {len(text)} characters; "
                f"you see the first {limit}. Ask for less data to see the rest.]"
            )

        return ModelTool.from_schema(
            call,
            name=model_tool_name(spec.name),
            description=spec.description,
            json_schema=spec.parameters,
            takes_ctx=True,
        )

    async def _finish(  # noqa: PLR0913
        self,
        state: _TurnState,
        messages: list[ModelMessage],
        *,
        status: TurnStatus,
        stop_reason: StopReason | None = None,
        error: str | None = None,
        output: Any = None,
    ) -> TurnResult:
        usage = _usage(messages, state.tool_calls)
        cleared = [
            part.tool_name
            for message in messages
            for part in message.parts
            if isinstance(part, ToolReturnPart) and part.content == PLACEHOLDER
        ]
        if cleared:
            await self._history.add_compaction(
                state.turn_id,
                "in_turn",
                {"cleared_tool_results": len(cleared), "tools": cleared},
            )
        await self._history.finish(
            state.turn_id,
            status=status,
            stop_reason=stop_reason,
            error=error,
            result=output,
            messages_json=ModelMessagesTypeAdapter.dump_json(messages).decode(),
            usage=usage,
        )
        return TurnResult(
            turn_id=state.turn_id,
            status=status,
            result=output,
            stop_reason=stop_reason,
            error=error,
            usage=usage,
        )

    async def _after_turn(self, agent: Agent, source_key: str, turn_id: str) -> None:
        record = await self._history.get(turn_id)
        if record is not None:
            await self._history.index_turn(record)
        await self.summaries.update(agent, source_key, turn_id)

    def _spawn(self, coro: Any) -> None:
        task: asyncio.Task[None] = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task[None]) -> None:
        self._background.discard(task)
        if not task.cancelled() and (err := task.exception()) is not None:
            _LOG.error("background work after a turn failed", exc_info=err)

    async def drain(self) -> None:
        """Wait for the background work of finished turns (summaries)."""
        while self._background:
            await asyncio.gather(*list(self._background), return_exceptions=True)

    async def close(self) -> None:
        """Cancel the background work."""
        for task in list(self._background):
            task.cancel()
        await self.drain()


def _limit_reason(err: UsageLimitExceeded) -> StopReason:
    text = str(err)
    if "tool_calls_limit" in text:
        return "tool_call_limit"
    if "tokens_limit" in text:
        return "token_limit"
    return "request_limit"


def _usage(messages: list[ModelMessage], tool_calls: int) -> TurnUsage:
    responses = [m for m in messages if isinstance(m, ModelResponse)]
    return TurnUsage(
        requests=len(responses),
        tool_calls=tool_calls,
        input_tokens=sum(r.usage.input_tokens for r in responses),
        cache_read_tokens=sum(r.usage.cache_read_tokens for r in responses),
        cache_write_tokens=sum(r.usage.cache_write_tokens for r in responses),
        output_tokens=sum(r.usage.output_tokens for r in responses),
    )
