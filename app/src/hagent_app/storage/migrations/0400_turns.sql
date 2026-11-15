-- Agent turns and their history (range 0400-0499, task-10).
--
-- turns holds one row for each agent turn (design sections 9.5 and 13).
-- The runtime writes the row with status 'running' when the turn starts,
-- and sets the final status when it ends. messages holds the Pydantic AI
-- messages of the turn as JSON, after compaction.
--
-- source_key groups the turns that share a running summary: the source of
-- the invocation, or 'assist:<conversation ID>' for an Assist conversation.
--
-- turn_tool_calls holds each tool call of a turn with its full result. The
-- model can receive a shorter copy (cut = 1).
--
-- turn_compactions holds each compaction: a cut tool result, cleared tool
-- results inside a turn, a new running summary, or a request to shorten a
-- main file (design section 13.1).
--
-- source_summaries holds the running summary of each source of an agent.
-- covers_until is the start time of the last turn that the summary covers.

CREATE TABLE turns (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents (id),
    agent_version INTEGER NOT NULL,
    invocation_id TEXT,
    source TEXT NOT NULL,
    source_key TEXT NOT NULL,
    conversation_id TEXT,
    dry_run INTEGER NOT NULL CHECK (dry_run IN (0, 1)),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL
        CHECK (status IN ('running', 'ok', 'stopped', 'failed', 'cancelled')),
    stop_reason TEXT,
    error TEXT,
    input TEXT NOT NULL,
    instructions_sha256 TEXT NOT NULL,
    prompt TEXT NOT NULL,
    result TEXT,
    messages TEXT,
    requests INTEGER NOT NULL DEFAULT 0,
    tool_calls INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (agent_id, agent_version)
        REFERENCES agent_versions (agent_id, version)
) STRICT;

CREATE INDEX turns_agent_started ON turns (agent_id, started_at);
CREATE INDEX turns_agent_source ON turns (agent_id, source_key, started_at);
CREATE INDEX turns_invocation ON turns (invocation_id);

CREATE TABLE turn_tool_calls (
    turn_id TEXT NOT NULL REFERENCES turns (id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    tool_call_id TEXT,
    tool TEXT NOT NULL,
    arguments TEXT NOT NULL,
    ok INTEGER NOT NULL CHECK (ok IN (0, 1)),
    error_code TEXT,
    result TEXT NOT NULL,
    result_chars INTEGER NOT NULL,
    cut INTEGER NOT NULL CHECK (cut IN (0, 1)),
    audit_id INTEGER,
    PRIMARY KEY (turn_id, seq)
) STRICT;

CREATE TABLE turn_compactions (
    id INTEGER PRIMARY KEY,
    turn_id TEXT NOT NULL REFERENCES turns (id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (
        kind IN ('tool_result_cut', 'in_turn', 'running_summary', 'main_file_request')
    ),
    detail TEXT NOT NULL,
    summary TEXT
) STRICT;

CREATE INDEX turn_compactions_turn ON turn_compactions (turn_id);

CREATE TABLE source_summaries (
    agent_id TEXT NOT NULL REFERENCES agents (id),
    source_key TEXT NOT NULL,
    summary TEXT NOT NULL,
    covers_until TEXT NOT NULL,
    turn_count INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (agent_id, source_key)
) STRICT;
