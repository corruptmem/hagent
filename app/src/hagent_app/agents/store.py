"""SQL access for agents, definition versions and private store records.

Each function takes an open connection and runs on the database worker
thread (``Database.run``). Functions that write use one transaction, so a
failure leaves no partial change.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from hagent_app.agents.definition import AgentDefinition, KnowledgeAttachment
from hagent_app.agents.models import (
    Agent,
    AgentNotFoundError,
    AgentVersion,
    Change,
    DefinitionInvalidError,
    FieldChange,
    NameConflictError,
    StoreChoice,
    StoreChoiceRequiredError,
    StoreInUseError,
    StoreRef,
    VersionConflictError,
    VersionNotFoundError,
)
from hagent_app.storage import transaction

_AGENT_SELECT = """
SELECT a.id, a.name, a.enabled, a.current_version, a.created_at, a.updated_at,
       v.definition, s.id AS store_id, s.name AS store_name
FROM agents a
JOIN agent_versions v ON v.agent_id = a.id AND v.version = a.current_version
LEFT JOIN knowledge_stores s ON s.owner_agent_id = a.id AND s.private = 1
"""

_VERSION_SELECT = """
SELECT agent_id, version, definition, change, source_agent_id, source_version,
       created_at
FROM agent_versions
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


def validation_details(err: ValidationError) -> list[dict[str, Any]]:
    """The errors of a pydantic validation, in the API error format."""
    return [
        {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]}
        for e in err.errors(include_url=False, include_context=False)
    ]


def parse_definition(text: str | bytes) -> AgentDefinition:
    """Parse and validate a definition. Raise ``DefinitionInvalidError``."""
    try:
        return AgentDefinition.from_json(text)
    except ValidationError as err:
        msg = "the agent definition is not valid"
        raise DefinitionInvalidError(msg, validation_details(err)) from err


# ----------------------------------------------------------------- reads


def _agent(row: sqlite3.Row) -> Agent:
    store = (
        StoreRef(row["store_id"], row["store_name"])
        if row["store_id"] is not None
        else None
    )
    return Agent(
        id=row["id"],
        name=row["name"],
        enabled=bool(row["enabled"]),
        version=row["current_version"],
        definition=AgentDefinition.from_json(row["definition"]),
        private_store=store,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _version(row: sqlite3.Row) -> AgentVersion:
    return AgentVersion(
        agent_id=row["agent_id"],
        version=row["version"],
        definition=AgentDefinition.from_json(row["definition"]),
        change=row["change"],
        source_agent_id=row["source_agent_id"],
        source_version=row["source_version"],
        created_at=row["created_at"],
    )


def list_agents(conn: sqlite3.Connection) -> list[Agent]:
    """Every live agent, sorted by name."""
    rows = conn.execute(
        _AGENT_SELECT + " WHERE a.deleted_at IS NULL ORDER BY a.name COLLATE NOCASE"
    ).fetchall()
    return [_agent(r) for r in rows]


def find_agent(conn: sqlite3.Connection, agent_id: str) -> Agent | None:
    """The live agent with this ID, or ``None``."""
    row = conn.execute(
        _AGENT_SELECT + " WHERE a.id = ? AND a.deleted_at IS NULL", (agent_id,)
    ).fetchone()
    return _agent(row) if row is not None else None


def get_agent(conn: sqlite3.Connection, agent_id: str) -> Agent:
    """The live agent with this ID. Raise ``AgentNotFoundError``."""
    agent = find_agent(conn, agent_id)
    if agent is None:
        msg = f"no agent with ID {agent_id!r}"
        raise AgentNotFoundError(msg)
    return agent


def find_version(
    conn: sqlite3.Connection, agent_id: str, version: int
) -> AgentVersion | None:
    """One version, also of a deleted agent, or ``None``."""
    row = conn.execute(
        _VERSION_SELECT + " WHERE agent_id = ? AND version = ?", (agent_id, version)
    ).fetchone()
    return _version(row) if row is not None else None


def get_version(conn: sqlite3.Connection, agent_id: str, version: int) -> AgentVersion:
    """One version of a live agent. Raise if it does not exist."""
    get_agent(conn, agent_id)
    found = find_version(conn, agent_id, version)
    if found is None:
        msg = f"agent {agent_id!r} has no version {version}"
        raise VersionNotFoundError(msg)
    return found


def list_versions(conn: sqlite3.Connection, agent_id: str) -> list[AgentVersion]:
    """Every version of a live agent, newest first."""
    get_agent(conn, agent_id)
    rows = conn.execute(
        _VERSION_SELECT + " WHERE agent_id = ? ORDER BY version DESC", (agent_id,)
    ).fetchall()
    return [_version(r) for r in rows]


# ---------------------------------------------------------------- writes


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug[:40] or "agent"


def _store_name(conn: sqlite3.Connection, agent_name: str) -> str:
    base = f"{_slug(agent_name)}_private"
    name, n = base, 1
    while conn.execute(
        "SELECT 1 FROM knowledge_stores WHERE name = ? COLLATE NOCASE", (name,)
    ).fetchone():
        n += 1
        name = f"{base}_{n}"
    return name


def _check_stores(
    conn: sqlite3.Connection, definition: AgentDefinition, private_store_id: str
) -> None:
    """Check the knowledge attachments against the store table."""
    errors: list[dict[str, Any]] = []
    for i, attachment in enumerate(definition.knowledge):
        row = conn.execute(
            "SELECT 1 FROM knowledge_stores WHERE id = ?", (attachment.store_id,)
        ).fetchone()
        if row is None:
            errors.append(
                {
                    "loc": ["knowledge", i, "store_id"],
                    "msg": f"no knowledge store with ID {attachment.store_id!r}",
                    "type": "store_not_found",
                }
            )
        elif attachment.store_id == private_store_id and (
            attachment.access != "read_write"
        ):
            errors.append(
                {
                    "loc": ["knowledge", i, "access"],
                    "msg": "the private store needs read_write access",
                    "type": "private_store_access",
                }
            )
    if not any(a.store_id == private_store_id for a in definition.knowledge):
        errors.append(
            {
                "loc": ["knowledge"],
                "msg": f"the private store {private_store_id!r} must stay attached",
                "type": "private_store_missing",
            }
        )
    if errors:
        msg = "the agent definition is not valid"
        raise DefinitionInvalidError(msg, errors)


def _with_private_store(
    definition: AgentDefinition, store_id: str, replace: str | None = None
) -> AgentDefinition:
    """Attach the private store, or move the attachment from ``replace``."""
    attachments = list(definition.knowledge)
    for i, attachment in enumerate(attachments):
        if attachment.store_id in {store_id, replace}:
            attachments[i] = attachment.model_copy(update={"store_id": store_id})
            break
    else:
        attachments.insert(
            0,
            KnowledgeAttachment(
                store_id=store_id, access="read_write", inject_main=True
            ),
        )
    # Validate again, so that the cross-field rules see the new list.
    data = definition.to_data()
    data["knowledge"] = [a.model_dump(mode="json") for a in attachments]
    return AgentDefinition.model_validate(data, strict=False, by_alias=True)


def _insert_version(  # noqa: PLR0913, PLR0917 - one argument per column
    conn: sqlite3.Connection,
    agent_id: str,
    version: int,
    definition: AgentDefinition,
    change: Change,
    now: str,
    source: tuple[str, int] | None = None,
) -> None:
    conn.execute(
        "INSERT INTO agent_versions (agent_id, version, definition, change, "
        "source_agent_id, source_version, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            agent_id,
            version,
            definition.to_json(),
            change,
            source[0] if source else None,
            source[1] if source else None,
            now,
        ),
    )


def _name_conflict(name: str) -> NameConflictError:
    return NameConflictError(f"another agent has the name {name!r}")


def create_agent(
    conn: sqlite3.Connection,
    definition: AgentDefinition,
    *,
    change: Change = "create",
    source: tuple[str, int] | None = None,
    replace_store: str | None = None,
) -> Agent:
    """Create an agent, its private store and version 1."""
    agent_id, store_id, now = _new_id(), _new_id(), _now()
    with transaction(conn):
        try:
            conn.execute(
                "INSERT INTO agents (id, name, enabled, current_version, "
                "created_at, updated_at) VALUES (?, ?, 1, 1, ?, ?)",
                (agent_id, definition.name, now, now),
            )
        except sqlite3.IntegrityError as err:
            raise _name_conflict(definition.name) from err
        conn.execute(
            "INSERT INTO knowledge_stores (id, name, description, owner_agent_id, "
            "private, created_at) VALUES (?, ?, ?, ?, 1, ?)",
            (
                store_id,
                _store_name(conn, definition.name),
                f"Private notes of the agent {definition.name}.",
                agent_id,
                now,
            ),
        )
        definition = _with_private_store(definition, store_id, replace_store)
        _check_stores(conn, definition, store_id)
        _insert_version(conn, agent_id, 1, definition, change, now, source)
    return get_agent(conn, agent_id)


def update_agent(  # noqa: PLR0913
    conn: sqlite3.Connection,
    agent_id: str,
    definition: AgentDefinition,
    *,
    base_version: int | None = None,
    change: Change = "edit",
    source_version: int | None = None,
) -> tuple[Agent, bool]:
    """Add a new version. Return the agent and whether a version was added.

    A definition that equals the current one adds no version.
    """
    with transaction(conn):
        current = get_agent(conn, agent_id)
        if base_version is not None and base_version != current.version:
            msg = (
                f"the current version is {current.version}, "
                f"not the base version {base_version}"
            )
            raise VersionConflictError(msg, {"current_version": current.version})
        if current.private_store is not None:
            _check_stores(conn, definition, current.private_store.id)
        if definition.to_json() == current.definition.to_json():
            return current, False
        version, now = current.version + 1, _now()
        try:
            conn.execute(
                "UPDATE agents SET name = ?, current_version = ?, updated_at = ? "
                "WHERE id = ?",
                (definition.name, version, now, agent_id),
            )
        except sqlite3.IntegrityError as err:
            raise _name_conflict(definition.name) from err
        source = (agent_id, source_version) if source_version is not None else None
        _insert_version(conn, agent_id, version, definition, change, now, source)
    return get_agent(conn, agent_id), True


def restore_version(
    conn: sqlite3.Connection,
    agent_id: str,
    version: int,
    *,
    base_version: int | None = None,
) -> tuple[Agent, bool]:
    """Add a new version with the content of an earlier version.

    The stored definition is validated again, because the rules or the
    store table can have changed since the version was written.
    """
    old = conn.execute(
        "SELECT definition FROM agent_versions WHERE agent_id = ? AND version = ?",
        (agent_id, version),
    ).fetchone()
    get_agent(conn, agent_id)
    if old is None:
        msg = f"agent {agent_id!r} has no version {version}"
        raise VersionNotFoundError(msg)
    definition = parse_definition(old["definition"])
    return update_agent(
        conn,
        agent_id,
        definition,
        base_version=base_version,
        change="restore",
        source_version=version,
    )


def _copy_name(conn: sqlite3.Connection, name: str) -> str:
    base = f"{name[:56]} (copy)"
    candidate, n = base, 1
    while conn.execute(
        "SELECT 1 FROM agents WHERE name = ? COLLATE NOCASE AND deleted_at IS NULL",
        (candidate,),
    ).fetchone():
        n += 1
        candidate = f"{base[:60]} {n}"
    return candidate


def copy_agent(conn: sqlite3.Connection, agent_id: str, name: str | None) -> Agent:
    """Create a new agent from the current definition of another agent.

    The copy gets its own private store. Shared attachments stay.
    """
    source = get_agent(conn, agent_id)
    data = source.definition.to_data()
    data["name"] = name if name is not None else _copy_name(conn, source.name)
    try:
        definition = AgentDefinition.model_validate(data, strict=False, by_alias=True)
    except ValidationError as err:
        msg = "the agent definition is not valid"
        raise DefinitionInvalidError(msg, validation_details(err)) from err
    return create_agent(
        conn,
        definition,
        change="copy",
        source=(source.id, source.version),
        replace_store=source.private_store.id if source.private_store else None,
    )


def set_enabled(conn: sqlite3.Connection, agent_id: str, enabled: bool) -> Agent:
    """Turn the agent on or off. This adds no definition version."""
    with transaction(conn):
        get_agent(conn, agent_id)
        conn.execute(
            "UPDATE agents SET enabled = ?, updated_at = ? WHERE id = ?",
            (int(enabled), _now(), agent_id),
        )
    return get_agent(conn, agent_id)


def delete_agent(
    conn: sqlite3.Connection, agent_id: str, private_store: StoreChoice | None
) -> None:
    """Delete an agent. Keep or delete its private store, as the user chose.

    The agent row and its versions stay for history, marked as deleted.
    """
    if private_store not in {"keep", "delete"}:
        msg = "say whether to keep or delete the private store: keep or delete"
        raise StoreChoiceRequiredError(msg, {"choices": ["keep", "delete"]})
    with transaction(conn):
        agent = get_agent(conn, agent_id)
        store = agent.private_store
        if store is not None and private_store == "delete":
            users = [
                a.id
                for a in list_agents(conn)
                if a.id != agent_id
                and any(k.store_id == store.id for k in a.definition.knowledge)
            ]
            if users:
                msg = "other agents attach the private store"
                raise StoreInUseError(msg, {"agent_ids": users})
            conn.execute("DELETE FROM knowledge_stores WHERE id = ?", (store.id,))
        elif store is not None:
            conn.execute(
                "UPDATE knowledge_stores SET private = 0, owner_agent_id = NULL "
                "WHERE id = ?",
                (store.id,),
            )
        now = _now()
        conn.execute(
            "UPDATE agents SET enabled = 0, deleted_at = ?, updated_at = ? "
            "WHERE id = ?",
            (now, now, agent_id),
        )


# ------------------------------------------------------------------ diff


def _flatten(value: Any, prefix: str, out: dict[str, Any]) -> None:
    if isinstance(value, dict) and value:
        for key, item in value.items():
            _flatten(item, f"{prefix}.{key}" if prefix else key, out)
    else:
        out[prefix] = value


def diff_definitions(old: AgentDefinition, new: AgentDefinition) -> list[FieldChange]:
    """The changed fields, sorted by path. A list counts as one value."""
    a: dict[str, Any] = {}
    b: dict[str, Any] = {}
    _flatten(old.to_data(), "", a)
    _flatten(new.to_data(), "", b)
    return [
        FieldChange(path, a.get(path), b.get(path))
        for path in sorted(a.keys() | b.keys())
        if a.get(path) != b.get(path)
    ]
