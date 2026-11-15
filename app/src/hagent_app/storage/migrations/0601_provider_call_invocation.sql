-- Link each provider call to its invocation (task-12).
--
-- invocation_id is the invocation that caused the call, or NULL for a call
-- outside an invocation (for example a document embedding). The router
-- sums cost_pusd by invocation_id to report the cost of an invocation.

ALTER TABLE provider_calls ADD COLUMN invocation_id TEXT;

CREATE INDEX provider_calls_invocation ON provider_calls (invocation_id);
