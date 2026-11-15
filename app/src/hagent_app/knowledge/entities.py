"""The HA entity catalogue in the search index (design section 13.3).

The **entity catalogue** is one search text for each HA entity: its ID, its
names and aliases, its area, its device name, its labels and its domain.
The app builds it again after each registry reload. Embeddings of the
catalogue count towards the installation only.

``EntityCatalog`` implements the ``EntitySearch`` protocol of the tool
executor. ``ha.find_entities`` passes only the entities in the agent's
scope. The search then ranks those entities and no others.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence

from hagent_app.ha import StateCache
from hagent_app.knowledge.index import IndexEntry, Scope, SearchIndex, replace_source
from hagent_app.knowledge.text import Passage
from hagent_app.storage import Database, transaction
from hagent_app.tools.registry import RegistrySnapshot


class CatalogNotReadyError(RuntimeError):
    """The catalogue is empty. The caller uses its own word match."""


def entity_texts(
    snapshot: RegistrySnapshot | None, states: StateCache
) -> dict[str, tuple[str, str]]:
    """The (title, search text) of each known entity."""
    ids = {s.entity_id for s in states}
    if snapshot is not None:
        ids |= set(snapshot.entities)
    result: dict[str, tuple[str, str]] = {}
    for entity_id in sorted(ids):
        state = states.get(entity_id)
        friendly = str(state.attributes.get("friendly_name", "")) if state else ""
        entry = snapshot.entities.get(entity_id) if snapshot else None
        device = (
            snapshot.devices.get(entry.device_id)
            if snapshot and entry and entry.device_id
            else None
        )
        area_id = (entry.area_id if entry else None) or (
            device.area_id if device else None
        )
        area = snapshot.areas.get(area_id) if snapshot and area_id else None
        labels = set(entry.labels if entry else ()) | set(
            device.labels if device else ()
        )
        label_names = sorted(
            snapshot.label_names.get(lb, lb) if snapshot else lb for lb in labels
        )
        names = [n for n in (friendly, entry.name if entry else None) if n]
        lines = [
            entity_id,
            *dict.fromkeys(names),
            *(entry.aliases if entry else ()),
            f"area: {area.name or area.id}" if area else "",
            f"device: {device.name}" if device and device.name else "",
            f"labels: {', '.join(label_names)}" if label_names else "",
            f"domain: {entity_id.split('.', 1)[0]}",
        ]
        title = names[0] if names else entity_id
        result[entity_id] = (title, "\n".join(line for line in lines if line))
    return result


class EntityCatalog:
    """Builds the entity catalogue and ranks entities."""

    def __init__(
        self,
        db: Database,
        index: SearchIndex,
        states: StateCache,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self._db = db
        self._index = index
        self._states = states
        self._on_change = on_change
        self.size = 0

    async def rebuild(self, snapshot: RegistrySnapshot | None) -> int:
        """Replace the catalogue. Return the number of entities."""
        texts = entity_texts(snapshot, self._states)

        def write(conn: sqlite3.Connection) -> int:
            with transaction(conn):
                conn.execute("DELETE FROM search_passages WHERE collection = 'entity'")
                for entity_id, (title, text) in texts.items():
                    replace_source(
                        conn,
                        IndexEntry("entity", entity_id, title=title),
                        [Passage(0, None, None, text)],
                    )
            return len(texts)

        self.size = await self._db.run(write)
        if self._on_change is not None:
            self._on_change()
        return self.size

    async def rank_entities(
        self, query: str, entity_ids: Sequence[str], limit: int
    ) -> list[str]:
        """The IDs from ``entity_ids`` that match ``query``, best first."""
        if self.size == 0:
            msg = "the entity catalogue is empty"
            raise CatalogNotReadyError(msg)
        scope = Scope(("entity",), source_ids=tuple(entity_ids))
        result = await self._index.search(scope, query, limit)
        allowed = set(entity_ids)
        return [h.source_id for h in result.hits if h.source_id in allowed]
