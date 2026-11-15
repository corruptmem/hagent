"""Tests of the agent turn (task-10): prompt order, tools, limits, history.

The models are Pydantic AI ``FunctionModel`` instances. No test calls a
provider or a live HA instance.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from hagent_app.agents import Agent
from hagent_app.agents.definition import AgentDefinition, ModelRef
from hagent_app.providers.errors import BudgetExceededError
from hagent_app.turns import (
    TurnHistory,
    TurnInput,
    TurnRunner,
    TurnsConfig,
    model_tool_name,
)
from hagent_app.turns.runner import PLACEHOLDER
from helpers.tools import Harness, harness
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

NOW = datetime.fromisoformat("2026-11-15T08:00:00+00:00")
STRUCTURED = {
    "type": "object",
    "properties": {"target": {"type": "number"}},
    "required": ["target"],
}
TWO_MODELS = {
    "chat": {"provider": "openai", "model": "chat-model"},
    "summary": {"provider": "openai", "model": "summary-model"},
}
HA_READ = {"tools__ha__level": "read", "tools__ha__scope": {"areas": ["kitchen"]}}


@pytest.fixture
async def h(tmp_path: Path) -> AsyncIterator[Harness]:
    async with harness(tmp_path) as value:
        yield value


@dataclass
class Call:
    """One model request that a ``FunctionModel`` received."""

    messages: list[ModelMessage]
    info: AgentInfo

    @property
    def user_prompt(self) -> str:
        first = self.messages[0]
        assert isinstance(first, ModelRequest)
        parts = [p for p in first.parts if isinstance(p, UserPromptPart)]
        return str(parts[0].content)

    @property
    def tools(self) -> list[tuple[str, str, Any]]:
        return [
            (t.name, t.description or "", t.parameters_json_schema)
            for t in self.info.function_tools
        ]


type Reply = Callable[[Call], ModelResponse]


@dataclass
class Script:
    """A scripted model. ``replies`` answers each request in turn."""

    replies: list[Reply] = field(default_factory=list)
    default: Reply | None = None
    calls: list[Call] = field(default_factory=list)

    def model(self, name: str = "script") -> FunctionModel:
        def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            call = Call(list(messages), info)
            self.calls.append(call)
            if self.replies:
                return self.replies.pop(0)(call)
            assert self.default is not None, "the script has no more replies"
            return self.default(call)

        return FunctionModel(respond, model_name=name)


def text(value: str) -> Reply:
    return lambda _call: ModelResponse(parts=[TextPart(value)])


def tool(name: str, args: Mapping[str, Any], call_id: str = "c1") -> Reply:
    return lambda _call: ModelResponse(
        parts=[ToolCallPart(model_tool_name(name), dict(args), tool_call_id=call_id)]
    )


def final(data: Mapping[str, Any]) -> Reply:
    def reply(call: Call) -> ModelResponse:
        [output] = call.info.output_tools
        return ModelResponse(parts=[ToolCallPart(output.name, dict(data))])

    return reply


class Models:
    """A model factory that maps each model reference to a script."""

    def __init__(self, chat: Script, summary: Script | None = None) -> None:
        self.chat = chat
        self.summary = summary or Script(default=text("SUMMARY"))
        self.used: list[str] = []

    def __call__(self, ref: ModelRef, agent_id: str) -> Model:
        self.used.append(ref.model)
        if ref.model == "summary-model":
            return self.summary.model("summary")
        return self.chat.model("chat")


async def make_agent(h: Harness, *, main: bool = False, **changes: Any) -> Agent:
    """Create an agent. With ``main``, attach its private store with MAIN.md."""
    agent = await h.agent(**changes)
    if not main:
        return agent
    assert agent.private_store is not None
    data = agent.definition.to_data()
    data["knowledge"] = [
        {
            "store_id": agent.private_store.id,
            "access": "read_write",
            "inject_main": True,
        }
    ]
    return await h.agents.update(agent.id, AgentDefinition.from_json(json.dumps(data)))


def runner(h: Harness, models: Models, **config: Any) -> TurnRunner:
    return TurnRunner(
        lambda: h.executor, TurnHistory(h.db), models, TurnsConfig(**config)
    )


def set_store(
    h: Harness, *, main_text: str = "Main notes.", items: list[Any] | None = None
) -> None:
    """Give the fake knowledge store a description, an index and MAIN.md."""

    async def describe(store_ids: Any) -> list[dict[str, Any]]:
        return [
            {"id": s, "name": "notes", "description": "My notes."} for s in store_ids
        ]

    async def list_items(store_id: str) -> list[dict[str, Any]]:
        return list(items or [])

    async def read(store_id: str, path: str, section: str | None) -> dict[str, Any]:
        return {"text": main_text if path == "MAIN.md" else "x" * 20_000}

    h.knowledge.describe = describe  # type: ignore[method-assign]
    h.knowledge.list_items = list_items  # type: ignore[method-assign]
    h.knowledge.read = read  # type: ignore[method-assign]


# ------------------------------------------------------- AC1: prompt order


async def test_prompt_follows_section_9_5_and_prefix_is_stable(h: Harness) -> None:
    agent = await make_agent(h, main=True, **HA_READ)
    set_store(h, items=[{"path": "b.md"}, {"path": "a.md", "title": "Alpha"}])
    chat = Script(default=text("done"))
    run = runner(h, Models(chat))

    first = await run.run_turn(
        agent,
        TurnInput("invoke", inputs={"sensor.t": "19.5"}, data={"why": "a"}, now=NOW),
    )
    second = await run.run_turn(
        agent,
        TurnInput(
            "invoke",
            inputs={"sensor.t": "21.0"},
            data={"why": "b"},
            now=NOW + timedelta(minutes=5),
        ),
    )
    assert first.ok and second.ok
    one, two = chat.calls

    # Parts 1 to 4 (tools and instructions) are identical across invocations.
    assert one.tools == two.tools
    assert one.info.instructions == two.info.instructions
    instructions = one.info.instructions or ""
    # The time, the inputs and the caller data are not in the stable prefix.
    for volatile in ("2026-11-15", "19.5", "21.0", '"why"'):
        assert volatile not in instructions
    # Tool definitions use provider-safe names.
    assert all("." not in name for name, _, _ in one.tools)
    assert "ha__get_state" in [name for name, _, _ in one.tools]

    # Parts 2, 3 and 4 in order. The index is sorted by name.
    order = [
        "# Instructions",
        "Keep the rooms warm.",
        "# Knowledge stores",
        "Description: My notes.",
        "- a.md: Alpha\n- b.md",
        "### Main file MAIN.md",
        "Main notes.",
        "# Your rules, schedules and scopes",
        "HA level: read.",
    ]
    positions = [instructions.index(marker) for marker in order]
    assert positions == sorted(positions)

    # Parts 5 and 7 in the user prompt: history first, the invocation last.
    assert "# Recent history" not in one.user_prompt
    prompt = two.user_prompt
    assert prompt.index("# Recent history of source invoke") < prompt.index(
        "# Invocation"
    )
    assert '{"sensor.t":"19.5"}' in prompt.split("# Invocation")[0]
    invocation = prompt.split("# Invocation")[1]
    assert "Time: 2026-11-15T08:05:00+00:00" in invocation
    assert 'Inputs: {"sensor.t":"21.0"}' in invocation
    assert 'Caller data: {"why":"b"}' in invocation

    # History records the hash of the stable part for each turn.
    history = TurnHistory(h.db)
    a = await history.get(first.turn_id)
    b = await history.get(second.turn_id)
    assert a is not None and b is not None
    assert a.instructions_sha256 == b.instructions_sha256


async def test_recall_goes_after_history_and_before_the_invocation(
    h: Harness,
) -> None:
    agent = await make_agent(h, main=True, retrieval={"mode": "all"})
    set_store(h)

    async def search(store_ids: Any, query: str, limit: int) -> list[dict[str, Any]]:
        return [{"path": "a.md", "text": f"about {query}"}]

    h.knowledge.search = search  # type: ignore[method-assign]
    chat = Script(default=text("done"))
    run = runner(h, Models(chat))
    await run.run_turn(agent, TurnInput("invoke", data={"text": "boiler"}, now=NOW))
    await run.run_turn(agent, TurnInput("invoke", data={"text": "boiler"}, now=NOW))
    prompt = chat.calls[1].user_prompt
    assert (
        prompt.index("# Recent history")
        < prompt.index("# Recall")
        < prompt.index("# Invocation")
    )
    assert "about boiler" in prompt
    assert "# Recall" not in (chat.calls[1].info.instructions or "")


# ---------------------------------------------------- AC2: large results


async def test_large_tool_result_is_cut_and_kept_in_full(h: Harness) -> None:
    agent = await make_agent(h, main=True)
    assert agent.private_store is not None
    set_store(h)
    chat = Script(
        replies=[
            tool(
                "knowledge.read",
                {"store_id": agent.private_store.id, "path": "big.md"},
            ),
            text("read it"),
        ]
    )
    run = runner(h, Models(chat), tool_result_max_chars=500)
    result = await run.run_turn(agent, TurnInput("invoke", now=NOW))
    assert result.ok, result.error

    # The model saw at most the limit, with a marker.
    returns = [
        p
        for m in chat.calls[1].messages
        for p in m.parts
        if isinstance(p, ToolReturnPart)
    ]
    [seen] = returns
    assert isinstance(seen.content, str)
    assert seen.content.startswith('{"ok": true')
    assert "[cut: the result has" in seen.content
    assert len(seen.content) < 700

    # History keeps the full result.
    record = await TurnHistory(h.db).get(result.turn_id)
    assert record is not None
    [call] = record.tool_call_records
    assert call.tool == "knowledge.read"
    assert call.cut
    assert call.result["data"]["text"] == "x" * 20_000
    assert call.result_chars > 20_000
    assert [c.kind for c in record.compactions] == ["tool_result_cut"]


async def test_in_turn_compaction_clears_old_tool_results(h: Harness) -> None:
    agent = await make_agent(h, main=True)
    assert agent.private_store is not None
    set_store(h)
    args = {"store_id": agent.private_store.id, "path": "big.md"}
    chat = Script(
        replies=[tool("knowledge.read", args, f"c{i}") for i in range(4)]
        + [text("done")]
    )
    run = runner(
        h,
        Models(chat),
        compaction_target_tokens=2_000,
        compaction_min_clear_tokens=100,
        compaction_keep_pairs=1,
    )
    result = await run.run_turn(agent, TurnInput("invoke", now=NOW))
    assert result.ok, result.error
    last = chat.calls[-1].messages
    cleared = [
        p
        for m in last
        for p in m.parts
        if isinstance(p, ToolReturnPart) and p.content == PLACEHOLDER
    ]
    assert cleared
    record = await TurnHistory(h.db).get(result.turn_id)
    assert record is not None
    in_turn = [c for c in record.compactions if c.kind == "in_turn"]
    assert in_turn and in_turn[0].detail["cleared_tool_results"] >= 1
    # History still has every full result.
    assert len(record.tool_call_records) == 4
    assert all(c.result["ok"] for c in record.tool_call_records)


# ------------------------------------------------- AC3: running summaries


async def test_summary_model_writes_running_summary_per_source(h: Harness) -> None:
    agent = await make_agent(h, models=TWO_MODELS)
    chat = Script(default=text("answer"))
    summary = Script(default=text("SUMMARY-OF-OLD-TURNS"))
    models = Models(chat, summary)
    run = runner(h, models, recent_records=1, summary_batch=1)

    for i in range(3):
        await run.run_turn(agent, TurnInput("invoke", data={"n": i}, now=NOW))
        await run.drain()

    history = TurnHistory(h.db)
    stored = await history.summary(agent.id, "invoke")
    assert stored is not None
    assert stored.summary == "SUMMARY-OF-OLD-TURNS"
    assert stored.turn_count == 2
    # The summary model wrote it, from the two older records.
    [request] = summary.calls
    assert '"n":0' in request.user_prompt and '"n":1' in request.user_prompt
    assert '"n":2' not in request.user_prompt
    assert "running summary" in (request.info.instructions or "")
    assert all("running summary" not in (c.info.instructions or "") for c in chat.calls)

    # The next prompt holds the summary plus the latest record only.
    await run.run_turn(agent, TurnInput("invoke", data={"n": 3}, now=NOW))
    prompt = chat.calls[-1].user_prompt
    assert "Summary of earlier invocations (2):\nSUMMARY-OF-OLD-TURNS" in prompt
    history_part = prompt.split("# Invocation")[0]
    assert '"n":2' in history_part
    assert '"n":0' not in history_part

    # The trace shows the compaction and the summary that replaced the text.
    turns = await history.list_turns(agent.id)
    compactions = [
        c
        for t in turns
        for c in (await history.get(t.id) or t).compactions
        if c.kind == "running_summary"
    ]
    [compaction] = compactions
    assert compaction.summary == "SUMMARY-OF-OLD-TURNS"
    assert compaction.detail["model"]["model"] == "summary-model"


async def test_each_assist_conversation_has_its_own_summary(h: Harness) -> None:
    agent = await make_agent(h, models=TWO_MODELS)
    models = Models(Script(default=text("hi")), Script(default=text("CONV-1")))
    run = runner(h, models, recent_records=1, summary_batch=1)
    for i in range(3):
        await run.run_turn(
            agent, TurnInput("assist", data={"text": f"q{i}"}, conversation_id="c1")
        )
    await run.run_turn(
        agent, TurnInput("assist", data={"text": "other"}, conversation_id="c2")
    )
    await run.drain()
    history = TurnHistory(h.db)
    one = await history.summary(agent.id, "assist:c1")
    assert one is not None and one.summary == "CONV-1"
    assert await history.summary(agent.id, "assist:c2") is None
    # The second conversation does not see the first one.
    await run.run_turn(
        agent, TurnInput("assist", data={"text": "again"}, conversation_id="c2")
    )
    prompt = models.chat.calls[-1].user_prompt
    assert "q0" not in prompt and "CONV-1" not in prompt
    assert '"text":"other"' in prompt


async def test_summary_defaults_to_the_chat_model(h: Harness) -> None:
    agent = await make_agent(h)
    chat = Script(default=text("CHAT-SUMMARY"))
    models = Models(chat)
    run = runner(h, models, recent_records=1, summary_batch=1)
    for _ in range(3):
        await run.run_turn(agent, TurnInput("invoke"))
    await run.drain()
    stored = await TurnHistory(h.db).summary(agent.id, "invoke")
    assert stored is not None and stored.summary == "CHAT-SUMMARY"
    assert models.summary.calls == []


async def test_dry_runs_stay_out_of_the_running_history(h: Harness) -> None:
    agent = await make_agent(h)
    models = Models(Script(default=text("hi")))
    run = runner(h, models, recent_records=5)
    await run.run_turn(agent, TurnInput("invoke", data={"t": "dry"}, dry_run=True))
    await run.run_turn(agent, TurnInput("invoke", data={"t": "real"}))
    assert "# Recent history" not in models.chat.calls[-1].user_prompt


# ------------------------------------------------------ AC4: main files


@pytest.mark.parametrize(("chars", "asked"), [(85, True), (79, False)])
async def test_main_file_near_limit_asks_the_agent_to_shorten_it(
    h: Harness, chars: int, asked: bool
) -> None:
    agent = await make_agent(h, main=True)
    set_store(h, main_text="m" * chars)
    chat = Script(default=text("ok"))
    run = runner(h, Models(chat), main_file_max_chars=100)
    result = await run.run_turn(agent, TurnInput("invoke", now=NOW))
    [call] = chat.calls
    invocation = call.user_prompt.split("# Invocation")[1]
    assert ("rewrite MAIN.md shorter" in invocation) is asked
    # The request is not part of the stable prefix.
    assert "rewrite MAIN.md shorter" not in (call.info.instructions or "")
    # The runtime does not cut the file itself.
    assert "m" * chars in (call.info.instructions or "")
    record = await TurnHistory(h.db).get(result.turn_id)
    assert record is not None
    kinds = [c.kind for c in record.compactions]
    assert (kinds == ["main_file_request"]) is asked
    if asked:
        assert record.compactions[0].detail["chars"] == chars


# -------------------------------------------------- AC5: structured results


async def test_structured_result_gets_one_correction(h: Harness) -> None:
    agent = await make_agent(h, result={"type": "structured", "schema": STRUCTURED})
    chat = Script(replies=[final({"target": "warm"}), final({"target": 21.5})])
    result = await runner(h, Models(chat)).run_turn(agent, TurnInput("invoke"))
    assert result.ok, result.error
    assert result.result == {"target": 21.5}
    assert len(chat.calls) == 2
    # The correction request tells the model what failed.
    retry = chat.calls[1].messages[-1]
    assert "does not match the result schema" in str(retry.parts)


async def test_structured_result_fails_after_one_correction(h: Harness) -> None:
    agent = await make_agent(h, result={"type": "structured", "schema": STRUCTURED})
    chat = Script(default=final({"target": "warm"}))
    result = await runner(h, Models(chat)).run_turn(agent, TurnInput("invoke"))
    assert result.status == "failed"
    assert result.stop_reason == "invalid_result"
    assert len(chat.calls) == 2


async def test_caller_schema_replaces_the_result_type(h: Harness) -> None:
    agent = await make_agent(h)
    chat = Script(replies=[final({"target": 20})])
    result = await runner(h, Models(chat)).run_turn(
        agent, TurnInput("ai_task", result_schema=STRUCTURED)
    )
    assert result.ok and result.result == {"target": 20}


async def test_result_type_none_returns_no_result(h: Harness) -> None:
    agent = await make_agent(h, result={"type": "none"})
    result = await runner(h, Models(Script(default=text("x")))).run_turn(
        agent, TurnInput("invoke")
    )
    assert result.ok and result.result is None


# ------------------------------------------------------- AC6: turn limits


async def test_turn_stops_at_its_tool_call_limit(h: Harness) -> None:
    agent = await make_agent(h, budget={"max_tool_calls": 3})
    chat = Script(default=tool("memory.search", {"query": "x"}))
    result = await runner(h, Models(chat)).run_turn(agent, TurnInput("invoke"))
    assert result.status == "stopped"
    assert result.stop_reason == "tool_call_limit"
    assert result.usage.tool_calls == 3
    record = await TurnHistory(h.db).get(result.turn_id)
    assert record is not None
    assert record.status == "stopped" and record.stop_reason == "tool_call_limit"
    # The executor ran exactly the permitted number of calls.
    assert len(record.tool_call_records) == 3


async def test_turn_stops_at_its_token_limit(h: Harness) -> None:
    agent = await make_agent(h, budget={"max_tokens": 1000})

    def big(call: Call) -> ModelResponse:
        return ModelResponse(
            parts=[ToolCallPart("memory__search", {"query": "x"})],
            usage=RequestUsage(input_tokens=700, output_tokens=400),
        )

    chat = Script(default=big)
    result = await runner(h, Models(chat)).run_turn(agent, TurnInput("invoke"))
    assert result.status == "stopped"
    assert result.stop_reason == "token_limit"
    assert result.usage.input_tokens == 700
    assert len(chat.calls) == 1


# ---------------------------------------------------- tools and failures


async def test_out_of_scope_tool_call_never_reaches_ha(h: Harness) -> None:
    agent = await make_agent(
        h,
        tools__ha__level="control",
        tools__ha__scope={"areas": ["kitchen"]},
    )
    chat = Script(
        replies=[
            tool(
                "ha.call_service",
                {
                    "domain": "light",
                    "service": "turn_on",
                    "target": {"entity_id": "light.bedroom"},
                },
            ),
            text("could not"),
        ]
    )
    h.reset()
    result = await runner(h, Models(chat)).run_turn(agent, TurnInput("invoke"))
    assert result.ok
    assert h.ha.commands_of("call_service") == []
    record = await TurnHistory(h.db).get(result.turn_id)
    assert record is not None
    [call] = record.tool_call_records
    assert not call.ok
    assert call.error_code is not None
    assert call.audit_id is not None
    entry = (await h.audit.entries(agent.id))[0]
    assert entry.decision == "denied"
    assert entry.invocation_id == result.turn_id


async def test_budget_failure_is_reported_to_the_router(h: Harness) -> None:
    agent = await make_agent(h)

    def broke(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        msg = "the daily budget is spent"
        raise BudgetExceededError(msg, "agent")

    run = TurnRunner(
        lambda: h.executor, TurnHistory(h.db), lambda _r, _a: FunctionModel(broke)
    )
    result = await run.run_turn(agent, TurnInput("invoke"))
    assert result.status == "failed"
    assert result.stop_reason == "budget"


async def test_cancelled_turn_is_recorded(h: Harness) -> None:
    agent = await make_agent(h)
    started = asyncio.Event()

    async def slow(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        started.set()
        await asyncio.sleep(60)
        return ModelResponse(parts=[TextPart("late")])

    history = TurnHistory(h.db)
    run = TurnRunner(lambda: h.executor, history, lambda _r, _a: FunctionModel(slow))
    task = asyncio.create_task(run.run_turn(agent, TurnInput("invoke")))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    [record] = await history.list_turns(agent.id)
    assert record.status == "cancelled"


# ------------------------------------------------------ history store


async def test_history_search_sees_only_the_agents_own_turns(h: Harness) -> None:
    one = await make_agent(h)
    two = await make_agent(h)
    run = runner(h, Models(Script(default=text("boiler is fine"))))
    await run.run_turn(one, TurnInput("invoke", data={"q": "boiler"}))
    history = TurnHistory(h.db)
    found = await history.search(one.id, "boiler", {}, 10)
    assert len(found) == 1
    assert found[0]["result"] == "boiler is fine"
    assert await history.search(two.id, "boiler", {}, 10) == []
    # LIKE wildcards in the query match only themselves.
    assert await history.search(one.id, "%", {}, 10) == []


class FakeIndex:
    """A ``HistoryIndex`` that records calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def index_history(self, agent_id: str, record: Mapping[str, Any]) -> None:
        self.calls.append(("history", agent_id, record["turn_id"]))

    async def index_conversation(
        self, agent_id: str, conversation_id: str, record: Mapping[str, Any]
    ) -> None:
        self.calls.append(("conversation", agent_id, conversation_id))

    async def search_history(
        self, agent_id: str, query: str, filters: Mapping[str, Any], limit: int
    ) -> list[dict[str, Any]]:
        self.calls.append(("search", agent_id, query))
        return [{"hit": query}]


async def test_history_index_receives_turns_and_serves_search(h: Harness) -> None:
    agent = await make_agent(h)
    history = TurnHistory(h.db)
    index = FakeIndex()
    history.index = index
    run = TurnRunner(lambda: h.executor, history, Models(Script(default=text("ok"))))
    result = await run.run_turn(agent, TurnInput("invoke"))
    await run.run_turn(agent, TurnInput("assist", conversation_id="c9"))
    await run.run_turn(agent, TurnInput("invoke", dry_run=True))
    await run.drain()
    assert index.calls == [
        ("history", agent.id, result.turn_id),
        ("conversation", agent.id, "c9"),
    ]
    assert await history.search(agent.id, "q", {}, 5) == [{"hit": "q"}]


async def test_component_plugs_history_into_the_tools() -> None:
    from hagent_app.providers import ProvidersComponent  # noqa: PLC0415
    from hagent_app.tools import ToolsComponent  # noqa: PLC0415
    from hagent_app.turns import TurnsComponent, TurnsUnavailableError  # noqa: PLC0415

    tools = ToolsComponent()
    turns = TurnsComponent(ProvidersComponent(), tools)
    assert tools.history is turns.history
    with pytest.raises(TurnsUnavailableError):
        turns.runner  # noqa: B018


async def test_rule_state_changes_do_not_break_the_stable_prefix(h: Harness) -> None:
    agent = await make_agent(h, tools__self=True)
    runs = {"n": 0}

    async def list_items(agent_id: str, kind: str) -> list[dict[str, Any]]:
        runs["n"] += 1
        return [
            {
                "id": f"{kind}-1",
                "name": f"a {kind}",
                "enabled": True,
                "last_run": f"2026-11-15T08:0{runs['n']}:00",
                "fires": runs["n"],
            }
        ]

    h.agent_state.list_items = list_items  # type: ignore[method-assign]
    chat = Script(default=text("done"))
    run = runner(h, Models(chat))
    await run.run_turn(agent, TurnInput("invoke", now=NOW))
    await run.run_turn(agent, TurnInput("invoke", now=NOW))
    one, two = chat.calls
    assert one.info.instructions == two.info.instructions
    instructions = one.info.instructions or ""
    assert '"name":"a rule"' in instructions
    assert "last_run" not in instructions


async def test_summary_does_not_skip_a_running_turn(h: Harness) -> None:
    agent = await make_agent(h, models=TWO_MODELS)
    models = Models(Script(default=text("ok")), Script(default=text("SUM")))
    run = runner(h, models, recent_records=1, summary_batch=1)
    history = TurnHistory(h.db)
    # An older turn of the same source that still runs.
    await history.begin(
        turn_id="old",
        agent_id=agent.id,
        agent_version=agent.version,
        invocation_id=None,
        source="invoke",
        source_key="invoke",
        conversation_id=None,
        dry_run=False,
        input_data={},
        instructions_sha256="x",
        prompt="",
    )
    for _ in range(3):
        await run.run_turn(agent, TurnInput("invoke"))
    await run.drain()
    assert await history.summary(agent.id, "invoke") is None
    assert models.summary.calls == []


async def test_component_start_fails_turns_from_the_last_run(
    h: Harness, tmp_path: Path
) -> None:
    from hagent_app.providers import ProvidersComponent  # noqa: PLC0415
    from hagent_app.tools import ToolsComponent  # noqa: PLC0415
    from hagent_app.turns import TurnsComponent  # noqa: PLC0415

    agent = await make_agent(h)
    history = TurnHistory(h.db)
    await history.begin(
        turn_id="stale",
        agent_id=agent.id,
        agent_version=agent.version,
        invocation_id=None,
        source="invoke",
        source_key="invoke",
        conversation_id=None,
        dry_run=False,
        input_data={},
        instructions_sha256="x",
        prompt="",
    )

    @dataclass
    class Ctx:
        db: Any
        config: Any

    from hagent_app.config import Config  # noqa: PLC0415

    turns = TurnsComponent(ProvidersComponent(), ToolsComponent())
    config = Config(ha_url="ws://unused", ha_token="", data_dir=tmp_path)
    await turns.start(Ctx(h.db, config))  # type: ignore[arg-type]
    record = await history.get("stale")
    assert record is not None
    assert record.status == "failed"
    await turns.stop()
