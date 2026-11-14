-- Providers and cost schema (range 0300-0399, task-9).
--
-- Money columns hold integer pico-USD (1e-12 USD). Some providers charge
-- 4.2e-8 USD per token, and a REAL column loses that precision in sums.
--
-- provider_calls holds one row for each provider call: chat, decision or
-- embedding. agent_id is NULL for a cost that belongs to no agent, for
-- example a document embedding. The installation budget counts all rows.
--
-- budget_day is the local date of the call (the app runs in the time zone
-- of HA). Budgets reset at local midnight.

CREATE TABLE provider_calls (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    budget_day TEXT NOT NULL,
    agent_id TEXT,
    provider TEXT NOT NULL,
    call_type TEXT NOT NULL CHECK (call_type IN ('chat', 'decision', 'embedding')),
    model TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ok', 'error')),
    error TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    -- The cost that counts against the budgets. cost_source 'reserved'
    -- means that no price was known, so the reserved amount counts.
    cost_pusd INTEGER NOT NULL DEFAULT 0,
    cost_source TEXT NOT NULL CHECK (cost_source IN ('provider', 'price_table', 'reserved', 'none')),
    -- The cost from the price table, and the cost that the provider reported.
    computed_cost_pusd INTEGER,
    provider_cost_pusd INTEGER,
    -- (provider - computed) / computed, when both exist.
    drift REAL,
    price_source TEXT,
    price_version TEXT,
    reserved_pusd INTEGER NOT NULL DEFAULT 0
) STRICT;

CREATE INDEX provider_calls_day ON provider_calls (budget_day, agent_id);

-- Cost held against the budgets while a call runs. The runtime deletes the
-- row when the call ends. The app deletes all rows on start, because no
-- call survives a restart.
CREATE TABLE budget_reservations (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    budget_day TEXT NOT NULL,
    agent_id TEXT,
    amount_pusd INTEGER NOT NULL CHECK (amount_pusd >= 0)
) STRICT;

CREATE INDEX budget_reservations_day ON budget_reservations (budget_day, agent_id);

-- The daily copy of the OpenRouter price lists. pricing holds the JSON
-- pricing object of the model, in USD per token, as OpenRouter sends it.
CREATE TABLE openrouter_prices (
    model TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('chat', 'embedding')),
    pricing TEXT NOT NULL,
    fetched_at TEXT NOT NULL
) STRICT;
