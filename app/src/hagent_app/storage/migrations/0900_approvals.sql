-- Approval requests and answers (range 0900-0999, task-16).
--
-- approval_requests holds one row for each tool call that the definition
-- marks for approval (design section 8). The row starts with status
-- 'pending'. Exactly one conditional UPDATE moves it to a final status, so
-- the first valid answer wins.
--
-- Final statuses:
--   accepted   an approver accepted the call.
--   rejected   an approver rejected the call.
--   timeout    no valid answer arrived before expires_at.
--   failed     no notification reached an approver, or the request failed.
--   cancelled  the agent turn stopped while it waited.
--   expired    the app stopped or restarted while the request was pending.
--
-- approvers holds the HA user IDs that could answer, as a JSON list.
-- targets holds the notify services that received the request. problems
-- holds the reasons why an approver or a registration got no notification.
--
-- approval_answers holds every answer that the app received for a known
-- request: valid, late, refused and duplicate answers. It is evidence for
-- a review of who tried to answer.

CREATE TABLE approval_requests (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    agent_version INTEGER NOT NULL,
    invocation_id TEXT,
    tool TEXT NOT NULL,
    summary TEXT NOT NULL,
    request TEXT NOT NULL,
    approvers TEXT NOT NULL,
    targets TEXT NOT NULL DEFAULT '[]',
    problems TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK (
        status IN (
            'pending', 'accepted', 'rejected', 'timeout',
            'failed', 'cancelled', 'expired'
        )
    ),
    finished_at TEXT,
    answered_by TEXT,
    answered_via TEXT CHECK (answered_via IN ('notification', 'api')),
    reason TEXT,
    FOREIGN KEY (agent_id, agent_version)
        REFERENCES agent_versions (agent_id, version)
) STRICT;

CREATE INDEX approval_requests_status ON approval_requests (status, created_at);
CREATE INDEX approval_requests_agent ON approval_requests (agent_id, created_at);

CREATE TABLE approval_answers (
    id INTEGER PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES approval_requests (id),
    received_at TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('accept', 'reject')),
    user_id TEXT,
    via TEXT NOT NULL CHECK (via IN ('notification', 'api')),
    outcome TEXT NOT NULL CHECK (
        outcome IN ('applied', 'late', 'not_approver', 'no_user', 'app_user')
    )
) STRICT;

CREATE INDEX approval_answers_request ON approval_answers (request_id);
