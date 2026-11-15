"""The knowledge part of each agent turn (design section 13.4).

Each agent turn includes, after the tool definitions:

- the attached stores, with their descriptions and access;
- an index of each store: the names of its files and documents, sorted by
  name, with no content. If a store has more entries than the limit (50 by
  default), the index shows the first entries and the total count;
- the main files (``MAIN.md``) that the attachments inject.

Task-10 builds the prompt and calls ``build_turn_context``. The text holds
no time or other value that changes between turns, so it changes only when
files are added, renamed or removed, when a main file changes, or when the
attachments change. This keeps the provider prompt cache working.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from hagent_app.agents.definition import AgentDefinition
from hagent_app.knowledge.service import MAIN_FILE, KnowledgeService

DEFAULT_INDEX_ENTRIES = 50


@dataclass(frozen=True, slots=True)
class StoreContext:
    """One attached store in the turn context."""

    store_id: str
    name: str
    description: str
    access: str
    entries: tuple[str, ...]
    total_entries: int
    main_file: str | None
    main_file_size: int | None
    size_limit: int


@dataclass(frozen=True, slots=True)
class TurnContext:
    """The knowledge part of one agent turn."""

    stores: tuple[StoreContext, ...] = ()
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)


async def build_turn_context(
    service: KnowledgeService,
    definition: AgentDefinition,
    *,
    index_entries: int = DEFAULT_INDEX_ENTRIES,
) -> TurnContext:
    """Build the store list, the store index and the injected main files."""
    attachments = {a.store_id: a for a in definition.knowledge}
    if not attachments:
        return TurnContext()
    described = await service.describe(list(attachments))
    described.sort(key=lambda s: (str(s["name"]).lower(), s["id"]))
    items = await service.items_for_index([s["id"] for s in described])
    inject = [s for s, a in attachments.items() if a.inject_main]
    mains = await service.main_files(inject) if inject else {}
    stores = []
    for s in described:
        attachment = attachments[s["id"]]
        paths = items.get(s["id"], [])
        main = mains.get(s["id"])
        stores.append(
            StoreContext(
                store_id=s["id"],
                name=s["name"],
                description=s["description"],
                access=attachment.access,
                entries=tuple(paths[:index_entries]),
                total_entries=len(paths),
                main_file=main,
                main_file_size=len(main.encode("utf-8")) if main is not None else None,
                size_limit=int(s["size_limit"]),
            )
        )
    return TurnContext(tuple(stores), render(stores), {"stores": len(stores)})


def render(stores: list[StoreContext]) -> str:
    """The text form of the turn context."""
    lines = ["## Knowledge stores", ""]
    for s in stores:
        description = " ".join(s.description.split()) or "(no description)"
        lines.append(f"- {s.name} (id {s.store_id}, {s.access}): {description}")
    lines += ["", "## Store index", ""]
    for s in stores:
        lines.append(f"### {s.name}")
        if not s.total_entries:
            lines.append("(empty)")
        lines.extend(f"- {path}" for path in s.entries)
        if s.total_entries > len(s.entries):
            lines.append(
                f"- ... {s.total_entries - len(s.entries)} more "
                f"({s.total_entries} entries in total; use knowledge.list)"
            )
        lines.append("")
    injected = [s for s in stores if s.main_file is not None]
    if injected:
        lines += ["## Main files", ""]
        for s in injected:
            lines += [f"### {s.name}/{MAIN_FILE}", "", (s.main_file or "").strip(), ""]
    return "\n".join(lines).rstrip() + "\n"
