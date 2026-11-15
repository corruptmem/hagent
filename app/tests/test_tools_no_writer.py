"""No tool can reach a definition writer (design section 7, task-7 AC #4).

``AgentReader`` holds the database, and ``Database.run`` can run any SQL, so
an object walk alone proves little. These tests prove two things:

- the tools package does not import ``AgentService``, the SQL module of the
  agents area, or the management API;
- no ``AgentService`` instance is reachable from the executor or from a
  running tools component. The walk stops at the shared core services
  (``HAClient``, ``Database``, ``ChangeEvents``), because every component
  registers callbacks there.
"""

from __future__ import annotations

import ast
import asyncio
import concurrent.futures
import gc
import threading
import types
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from hagent_app.agents import AgentService
from hagent_app.app import App
from hagent_app.config import Config
from hagent_app.ha import HAClient
from hagent_app.http import ChangeEvents
from hagent_app.storage import Database
from hagent_app.tools import ToolsComponent
from helpers.fake_ha import FakeHA
from helpers.tools import Harness, harness

TOOLS_DIR = Path(__file__).resolve().parents[1] / "src" / "hagent_app" / "tools"
FORBIDDEN_MODULES = {
    "hagent_app.agents.service",
    "hagent_app.agents.store",
    "hagent_app.agents.api",
    "hagent_app.agents.component",
}
FORBIDDEN_NAMES = {"AgentService", "AgentsComponent", "AgentsApi", "store"}


def test_tools_package_imports_no_definition_writer() -> None:
    found = []
    for path in sorted(TOOLS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found += [
                    (path.name, a.name)
                    for a in node.names
                    if a.name in FORBIDDEN_MODULES
                ]
            elif isinstance(node, ast.ImportFrom):
                if node.module in FORBIDDEN_MODULES:
                    found.append((path.name, node.module))
                if node.module and node.module.startswith("hagent_app.agents"):
                    found += [
                        (path.name, a.name)
                        for a in node.names
                        if a.name in FORBIDDEN_NAMES
                    ]
    assert found == []


# Objects that link to the whole running program, not to the tool's own
# state: the event loop, its tasks and coroutine frames, and threads.
_STOP_TYPES: tuple[type, ...] = (
    type,
    types.ModuleType,
    types.FrameType,
    types.CoroutineType,
    asyncio.AbstractEventLoop,
    asyncio.Future,
    threading.Thread,
    concurrent.futures.Executor,
    # The shared core services. Every component receives them, and they
    # hold callbacks of every component. None of them writes definitions;
    # the import check above covers SQL access through the database.
    HAClient,
    Database,
    ChangeEvents,
)


def _referents(obj: object) -> list[object]:
    """The references of ``obj``. A function leads to its closure and its
    defaults, but not to its module globals, which reach the whole program.
    A bound method leads to its ``self``."""
    if isinstance(obj, types.FunctionType):
        refs: list[object] = [c.cell_contents for c in obj.__closure__ or ()]
        refs += list(obj.__defaults__ or ())
        refs += list((obj.__kwdefaults__ or {}).values())
        return refs
    return list(gc.get_referents(obj))


def _path_to(root: object, cls: type) -> list[str]:
    """The reference chain from ``root`` to an instance of ``cls``, or []."""
    parents: dict[int, tuple[object, int | None]] = {}
    stack: list[tuple[object, int | None]] = [(root, None)]
    while stack:
        obj, parent = stack.pop()
        if id(obj) in parents:
            continue
        parents[id(obj)] = (obj, parent)
        if isinstance(obj, cls):
            chain = []
            key: int | None = id(obj)
            while key is not None:
                item, key = parents[key]
                chain.append(type(item).__qualname__)
            return chain[::-1]
        if isinstance(obj, _STOP_TYPES):
            continue
        stack.extend((ref, id(obj)) for ref in _referents(obj))
    return []


@pytest.fixture
async def h(tmp_path: Path) -> AsyncIterator[Harness]:
    async with harness(tmp_path) as value:
        yield value


async def test_executor_reaches_no_agent_service(h: Harness) -> None:
    # The harness holds an AgentService, so one exists in this process.
    assert isinstance(h.agents, AgentService)
    assert _path_to(h.executor, AgentService) == []


async def test_walk_finds_a_planted_writer(h: Harness) -> None:
    """A control: the walk finds a writer in an attribute and in a closure."""
    holder = types.SimpleNamespace(service=h.agents)
    assert _path_to(holder, AgentService)[-1] == "AgentService"
    service = h.agents

    def closure() -> object:
        return service

    assert _path_to(types.SimpleNamespace(fn=closure), AgentService) != []
    assert _path_to(types.SimpleNamespace(fn=h.agents.update), AgentService) != []


async def test_running_component_reaches_no_agent_service(
    fake_ha: FakeHA, tmp_path: Path
) -> None:
    tools = ToolsComponent()
    config = Config(
        ha_url=fake_ha.url, ha_token=fake_ha.token, data_dir=tmp_path, port=0
    )
    from hagent_app.app import default_components  # noqa: PLC0415

    components = default_components()
    components.insert(-1, tools)
    app = App(config, components)
    await app.start()
    try:
        assert _path_to(tools.executor(), AgentService) == []
        assert tools.monitor is not None
        assert _path_to(tools.monitor, AgentService) == []
    finally:
        await app.stop()
