-- Settings schema (range 1000-1099, task-20).
--
-- app_settings holds the settings that the user changes in the management
-- UI (design section 7.2). Each row is one setting. value is JSON text.
-- A setting with no row takes its default from the app options.
--
-- API keys are rows with the key 'api_key.<provider>'. The API never
-- returns their value.

CREATE TABLE app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;
