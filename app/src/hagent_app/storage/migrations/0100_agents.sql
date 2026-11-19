-- Agents and definition versions (range 0100-0199, task-7).
--
-- agents holds one row for each agent. The row holds state that is not part
-- of the versioned definition: the enabled flag and the soft-delete time.
-- name mirrors the name in the current definition. A partial unique index
-- keeps the names of live agents unique and frees the name of a deleted
-- agent.
--
-- agent_versions holds every definition version. A version never changes.
-- An edit, a copy or a restore adds a row. Deleting an agent keeps its
-- versions, because history records refer to them (design section 10.6).
--
-- Tables of other areas that refer to agents(id) can use a plain foreign
-- key. The app never removes a row from agents.

CREATE TABLE agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    current_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
) STRICT;

CREATE UNIQUE INDEX agents_live_name
    ON agents (name COLLATE NOCASE)
    WHERE deleted_at IS NULL;

CREATE TABLE agent_versions (
    agent_id TEXT NOT NULL REFERENCES agents (id),
    version INTEGER NOT NULL CHECK (version >= 1),
    definition TEXT NOT NULL,
    change TEXT NOT NULL CHECK (change IN ('create', 'edit', 'copy', 'restore')),
    source_agent_id TEXT,
    source_version INTEGER,
    created_at TEXT NOT NULL,
    PRIMARY KEY (agent_id, version)
) STRICT;
