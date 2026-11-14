-- Core schema (range 0001-0099).
--
-- app_info holds facts about this installation as key and value pairs.
-- The app writes installation_id and created_at on the first start.
-- Later areas own their tables in their own ranges. See app/README.md.

CREATE TABLE app_info (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;
