-- The tool audit log and the resolved scopes (range 0200-0299, task-8).
--
-- tool_audit holds one row for each tool call that changes HA, and one row
-- for each denied call of any tool (design sections 7.2 and 8). The
-- executor writes the row with status 'pending' before it calls HA. If that
-- write fails, the executor does not call HA. After the call, it sets the
-- final status.
--
-- previous holds the previous version of the changed item for an admin
-- change, as JSON. previous_note says why previous is empty, if it is.

CREATE TABLE tool_audit (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    agent_id TEXT NOT NULL,
    agent_version INTEGER NOT NULL,
    invocation_id TEXT,
    tool TEXT NOT NULL,
    level TEXT NOT NULL,
    request TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('allowed', 'denied')),
    reason_code TEXT,
    reason TEXT,
    status TEXT NOT NULL
        CHECK (status IN ('pending', 'ok', 'error', 'denied', 'dry_run')),
    error TEXT,
    context_id TEXT,
    previous TEXT,
    previous_note TEXT,
    FOREIGN KEY (agent_id, agent_version)
        REFERENCES agent_versions (agent_id, version)
) STRICT;

CREATE INDEX tool_audit_agent ON tool_audit (agent_id, created_at);
CREATE INDEX tool_audit_decision ON tool_audit (decision, created_at);

-- The last resolved entity set of each agent version. The scope monitor
-- compares a new resolution with this row to find a scope that grew, also
-- across an app restart.

CREATE TABLE tool_scope_snapshots (
    agent_id TEXT PRIMARY KEY REFERENCES agents (id),
    agent_version INTEGER NOT NULL,
    read_entities TEXT NOT NULL,
    control_entities TEXT NOT NULL,
    resolved_at TEXT NOT NULL
) STRICT;
