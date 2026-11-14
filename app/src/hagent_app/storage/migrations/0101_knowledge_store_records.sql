-- The minimal knowledge store record (range 0100-0199, task-7).
--
-- Task-7 creates each agent's private store with the agent. Task-11 owns
-- knowledge stores (range 0500-0599). It extends this table with
-- ALTER TABLE, for example with the size limit, and adds the tables for
-- files and documents.
--
-- private = 1 marks the private store of owner_agent_id. Each agent has at
-- most one private store. When the user deletes an agent and keeps its
-- private store, the app sets private = 0 and owner_agent_id = NULL. The
-- store then is a normal shared store.

CREATE TABLE knowledge_stores (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    owner_agent_id TEXT REFERENCES agents (id),
    private INTEGER NOT NULL DEFAULT 0 CHECK (private IN (0, 1)),
    created_at TEXT NOT NULL,
    CHECK (private = 0 OR owner_agent_id IS NOT NULL)
) STRICT;

CREATE UNIQUE INDEX knowledge_stores_name
    ON knowledge_stores (name COLLATE NOCASE);

CREATE UNIQUE INDEX knowledge_stores_private_owner
    ON knowledge_stores (owner_agent_id)
    WHERE private = 1;
