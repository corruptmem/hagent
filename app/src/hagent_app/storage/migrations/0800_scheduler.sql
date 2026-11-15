-- Scheduler schema (range 0800-0899, task-15).
--
-- scheduler_items holds the schedules and subscriptions that agents create
-- with the self tools (design section 12). It is the source of truth. The
-- APScheduler job store keeps only the next fire time of each job, and the
-- app makes the job store match this table on each start.
--
-- kind is 'schedule' or 'subscription'. spec is the checked spec as JSON.
-- run_at is the due time of a timer (UTC, ISO 8601). It is NULL for a cron
-- schedule and for a subscription. The app deletes a timer after it fires
-- or after the app skips it.

CREATE TABLE scheduler_items (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents (id),
    kind TEXT NOT NULL CHECK (kind IN ('schedule', 'subscription')),
    spec TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    run_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE INDEX scheduler_items_agent ON scheduler_items (agent_id, kind);

-- scheduler_runs records each due time of a schedule or a review: a
-- submitted invocation, or a skip and its reason.
--
-- item_id is NULL for the review schedule of the definition. source is
-- 'schedule' or 'review'. status is:
--   'submitted'             the router accepted the invocation;
--   'refused'               the router refused it (result_code says why);
--   'skipped_missed'        the app was stopped and the run was 10 minutes
--                           late or more (design section 12);
--   'skipped_min_interval'  the agent had a scheduled invocation less than
--                           the minimum interval before;
--   'skipped_unavailable'   the router was not running.
-- missed_count is the number of due times that one row covers.

CREATE TABLE scheduler_runs (
    id INTEGER PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents (id),
    item_id TEXT,
    source TEXT NOT NULL CHECK (source IN ('schedule', 'review')),
    due_at TEXT NOT NULL,
    at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN (
            'submitted',
            'refused',
            'skipped_missed',
            'skipped_min_interval',
            'skipped_unavailable'
        )
    ),
    missed_count INTEGER NOT NULL DEFAULT 1,
    invocation_id TEXT,
    result_status TEXT,
    result_code TEXT
) STRICT;

CREATE INDEX scheduler_runs_agent ON scheduler_runs (agent_id, at);

-- scheduler_events records each HA event that matched a subscription, with
-- its origin (design section 12). origin is 'agent' when the context ID of
-- the event belongs to a service call of an agent, and 'external'
-- otherwise. origin_agent_id is the agent that made the call.
--
-- outcome is:
--   'delivered'   the event went to the agent in an invocation;
--   'own_write'   the subscriber caused the change, and the subscription
--                 ignores own writes;
--   'denied'      the event is outside the current scope of the agent;
--   'dropped'     the agent is deleted or off, or the router refused the
--                 invocation.
-- Several events in one debounce period share one invocation_id.

CREATE TABLE scheduler_events (
    id INTEGER PRIMARY KEY,
    subscription_id TEXT NOT NULL,
    agent_id TEXT NOT NULL REFERENCES agents (id),
    event_type TEXT NOT NULL,
    entity_id TEXT,
    context_id TEXT,
    origin TEXT NOT NULL CHECK (origin IN ('agent', 'external')),
    origin_agent_id TEXT,
    received_at TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (
        outcome IN ('delivered', 'own_write', 'denied', 'dropped')
    ),
    detail TEXT,
    invocation_id TEXT
) STRICT;

CREATE INDEX scheduler_events_agent ON scheduler_events (agent_id, received_at);
CREATE INDEX scheduler_events_entity ON scheduler_events (entity_id, received_at);
