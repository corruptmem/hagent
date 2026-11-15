-- Router and queue schema (range 0600-0699, task-12).
--
-- invocations holds one row for each invocation (design sections 9.4 and
-- 9.6). The row is also the persistent queue entry: the router writes it
-- with status 'queued' when the invocation waits, sets 'running' when it
-- starts, and sets the final status when it ends.
--
-- queue_key is the queue of the invocation: the agent ID, or
-- '<agent ID>/assist:<conversation ID>' for an Assist conversation.
--
-- agent_version and rule_version are the versions that were active when
-- the invocation started. They are NULL while the invocation waits.
--
-- path is 'rule', 'agent_turn' or 'fallback' (design section 9.2). It is
-- NULL for an invocation that did not run (rejected, busy, cancelled).
-- fallback_reason says why the router returned the fallback.
--
-- Money columns hold integer pico-USD (1e-12 USD), as in provider_calls.

CREATE TABLE invocations (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents (id),
    queue_key TEXT NOT NULL,
    source TEXT NOT NULL,
    source_key TEXT NOT NULL,
    conversation_id TEXT,
    dry_run INTEGER NOT NULL CHECK (dry_run IN (0, 1)),
    request TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    status TEXT NOT NULL CHECK (status IN (
        'queued', 'running', 'ok', 'fallback', 'error', 'cancelled'
    )),
    path TEXT CHECK (path IN ('rule', 'agent_turn', 'fallback')),
    fallback_reason TEXT,
    error_code TEXT,
    error TEXT,
    result TEXT,
    agent_version INTEGER,
    rule_id TEXT,
    rule_version INTEGER,
    turn_id TEXT,
    cost_pusd INTEGER NOT NULL DEFAULT 0
) STRICT;

CREATE INDEX invocations_status ON invocations (status, created_at);
CREATE INDEX invocations_agent ON invocations (agent_id, created_at);
CREATE INDEX invocations_last_result
    ON invocations (agent_id, source_key, status, dry_run, finished_at);
