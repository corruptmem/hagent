-- Invocation records, backtests and trials (range 0750-0799, task-14).
--
-- invocation_records holds what a backtest needs to replay one past
-- invocation (design section 10.7). The rule stage writes one row for each
-- invocation that a rule can answer:
--
-- - inputs: the input values, as {entity ID: state text or null}.
-- - states: the full input states, as {entity ID: {state, attributes,
--   last_changed, last_updated}}, so that state_attr() works in a replay.
-- - context: the context values that a rule collected, keyed by the JSON
--   form of the context item (without name and cache_seconds).
-- - decisions: the decision answers that a rule received, keyed by the
--   SHA-256 of the decision request.
--
-- The app deletes rows that are older than backtest.retention_days.
--
-- rule_backtests holds one row for each backtest of one rule version.
-- rule_backtest_cases holds one row for each past invocation that a
-- backtest tested. rule_trials holds one row for each trial: a dry-run
-- agent turn that runs next to a rule that answered (design section 10.1).
--
-- Money columns hold integer pico-USD (1e-12 USD), as in provider_calls.

CREATE TABLE invocation_records (
    invocation_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    source_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    inputs TEXT NOT NULL,
    states TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT '{}',
    decisions TEXT NOT NULL DEFAULT '{}'
) STRICT;

CREATE INDEX invocation_records_created ON invocation_records (created_at);

CREATE TABLE rule_backtests (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    agent_version INTEGER NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('running', 'ok', 'budget', 'failed', 'cancelled')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    since TEXT NOT NULL,
    until TEXT NOT NULL,
    max_cases INTEGER,
    candidates INTEGER NOT NULL DEFAULT 0,
    matched INTEGER NOT NULL DEFAULT 0,
    no_match INTEGER NOT NULL DEFAULT 0,
    compared INTEGER NOT NULL DEFAULT 0,
    agreed INTEGER NOT NULL DEFAULT 0,
    disagreed INTEGER NOT NULL DEFAULT 0,
    declined INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    live_decisions INTEGER NOT NULL DEFAULT 0,
    replayed_decisions INTEGER NOT NULL DEFAULT 0,
    cost_pusd INTEGER NOT NULL DEFAULT 0,
    error TEXT
) STRICT;

CREATE INDEX rule_backtests_rule ON rule_backtests (rule_id, version, started_at);

CREATE TABLE rule_backtest_cases (
    backtest_id TEXT NOT NULL REFERENCES rule_backtests (id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    invocation_id TEXT NOT NULL,
    invocation_at TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN (
        'agreed', 'disagreed', 'no_match', 'declined', 'error', 'skipped'
    )),
    decision TEXT CHECK (decision IN ('replayed', 'live')),
    rule_result TEXT,
    agent_result TEXT,
    writes TEXT,
    detail TEXT,
    PRIMARY KEY (backtest_id, seq)
) STRICT;

CREATE TABLE rule_trials (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    invocation_id TEXT NOT NULL,
    turn_id TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK (status IN (
        'running', 'agreed', 'disagreed', 'error', 'budget', 'cancelled'
    )),
    rule_result TEXT,
    agent_result TEXT,
    detail TEXT,
    cost_pusd INTEGER NOT NULL DEFAULT 0
) STRICT;

CREATE INDEX rule_trials_rule ON rule_trials (rule_id, version, created_at);
CREATE INDEX rule_trials_invocation ON rule_trials (invocation_id);
