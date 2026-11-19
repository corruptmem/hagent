-- Rules, rule versions and rule statistics (range 0700-0799, task-13).
--
-- A rule is a fast path that an agent writes with the self tools (design
-- section 10). rules holds one row for each rule:
--
-- - position: the order in which the router tests the rules of an agent.
-- - state: 'draft', 'active' or 'off'. Only active rules answer.
-- - current_version: the version that answers. A rollback points it to an
--   older version.
-- - latest_version: the highest version number of the rule.
-- - deleted_at: set when the agent deletes the rule. The row and its
--   versions stay, because invocation history names them.
--
-- rule_versions holds the spec of each version. A version never changes
-- (design section 10.6): a change to a rule adds a new row.
--
-- rule_stats holds the statistics of each version (design section 10.4).
-- Money columns hold integer pico-USD (1e-12 USD), as in provider_calls.

CREATE TABLE rules (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents (id),
    position INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('draft', 'active', 'off')),
    current_version INTEGER NOT NULL,
    latest_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
) STRICT;

CREATE INDEX rules_agent ON rules (agent_id, deleted_at, position);

CREATE TABLE rule_versions (
    rule_id TEXT NOT NULL REFERENCES rules (id),
    version INTEGER NOT NULL,
    spec TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (rule_id, version)
) STRICT;

CREATE TABLE rule_stats (
    rule_id TEXT NOT NULL REFERENCES rules (id),
    version INTEGER NOT NULL,
    matched INTEGER NOT NULL DEFAULT 0,
    answered INTEGER NOT NULL DEFAULT 0,
    declined INTEGER NOT NULL DEFAULT 0,
    cache_hits INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    cost_pusd INTEGER NOT NULL DEFAULT 0,
    last_used_at TEXT,
    last_error TEXT,
    last_error_at TEXT,
    PRIMARY KEY (rule_id, version),
    FOREIGN KEY (rule_id, version) REFERENCES rule_versions (rule_id, version)
) STRICT;
