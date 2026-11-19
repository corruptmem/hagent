---
id: TASK-21
title: Package the HA app and document manual installation
status: In Progress
assignee:
  - '@claude'
created_date: '2026-11-14 23:04'
updated_date: '2026-11-15 02:06'
labels:
  - packaging
dependencies:
  - TASK-6
  - TASK-17
  - TASK-18
references:
  - docs/design.md
priority: medium
type: chore
ordinal: 21000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Package hagent as a locally installable HA app (config.yaml with homeassistant_api and ingress, Dockerfile on the base image chosen in the image spike) and document how to install the app and copy the integration by hand.

Design: docs/design.md sections 6, 6.1 and 6.2.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 The app installs from a local app folder on HA OS and starts
- [ ] #2 The integration installs by copying into custom_components and connects to the app
- [x] #3 README documents both installation steps and first configuration
- [ ] #4 App data, including SQLite, is included in HA backups
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Add the app folder hagent/ at the repository root: config.yaml (amd64, ingress on 8099, homeassistant_api, discovery hagent, init false, backup cold, options and schema), build.yaml (base-debian trixie), Dockerfile, rootfs with the s6 service, DOCS.md, CHANGELOG.md, icon and logo placeholders, translations.
2. The Supervisor builds a local app with the app folder as the build context. Add scripts/package-app.sh. It copies the app folder and the sources (workspace files, app/, frontend/) into one self-contained folder.
3. Dockerfile: a node stage builds frontend/dist. The runtime stage installs uv CPython 3.14 and runs uv sync --frozen --no-dev in /opt/hagent with the source layout, so the UI default path (frontend/dist) resolves without a new option.
4. Validate config.yaml with the frenck app linter rules and the option schema with the Supervisor option validator, run locally.
5. Add spikes/task-21/smoke.sh: package, docker build linux/amd64, run FakeHA and the app in one network namespace, check migrations, the UI, /api and a restart.
6. README: installation of the app and the integration, first configuration, and a checklist for the items that need a real HA OS (task-6 AC 1, config/auth/list with SUPERVISOR_TOKEN, auth/current_user, backups).
7. mise run check, notes, summary. Leave AC 1, 2 and 4 for the user to verify.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Implementation:
- hagent/ is the app folder: config.yaml, Dockerfile, rootfs (s6 longrun service hagent, user bundle in user-bundles.d), translations/en.yaml, DOCS.md, CHANGELOG.md, placeholder icon.png and logo.png.
- The Supervisor builds a local app with the app folder as the build context. scripts/package-app.sh copies hagent/ and the sources (workspace files, app/, frontend/) into build/app/hagent. The user copies that folder into the local apps folder.
- No build.yaml. The Supervisor passes no BUILD_FROM without one (supervisor/apps/build.py), so the Dockerfile default applies: base-debian trixie-2026.11.0, uv 0.13.0, CPython 3.14.8. A Node 24.22.3 stage builds frontend/dist.
- /opt/hagent keeps the repository layout and uv sync installs the app as editable. The default ingress.ui_dir (frontend/dist next to app/) then resolves. No new option was needed.
- config.yaml: amd64 only, init false (s6 is PID 1), ingress (port 8099 by default), homeassistant_api, discovery [hagent], backup cold. hassio_api is not needed: the Supervisor lets every app post to /discovery (api_bypass in supervisor/api/middleware/security.py).
- Option schema: log_level, providers (API keys as password?, two budgets, genai_prices_auto_update), link (discovery, host). The Supervisor drops keys outside the schema and cannot express free maps, so approvers and the other settings stay on the Settings page.
- Backup: cold. A hot copy of hagent.db and hagent.db-wal can be inconsistent. backup_exclude of -wal loses writes. A backup_pre checkpoint does not stop later writes. Cost: agents pause during the backup; missed_run_grace_seconds covers short delays. README states the reasons.
- approvals: one INFO line logs the HA user ID of the app token, so that the user can compare it with the context user of a service call.
- Docs: README Installation section (integration, app, first configuration, backups, verification checklist), hagent/DOCS.md, app/README.md and frontend/README.md packaging notes.

Verification:
- app/tests/test_app_package.py (14 tests): the vendored JSON schema of frenck/action-app-linter at d5167729 with the linter default-value rule, the other lint.py rules, the Supervisor option rules (RE_SCHEMA_ELEMENT, nested dicts and lists, required keys, types) as a reimplementation of supervisor/apps/options.py at 98a23223, every schema key known to the app section dataclass, Supervisor-shaped options load in Config, translations cover the schema. Mutation checks: an unknown schema key and a default-valued key both fail the tests.
- The auto mode classifier refused an install of the Supervisor package from git. The option rules are therefore a reimplementation from the Supervisor source, not the Supervisor code itself.
- spikes/task-21/smoke.sh (mise run smoke-app) passed: package, docker build linux/amd64, FakeHA and the app on a bridge network, the app under s6 with SUPERVISOR_TOKEN, 13 migrations applied in WAL mode, sqlite-vec v0.1.9 loads, UI index 200, api/agents, api/settings and api/integration 200, link 401 without secret and hello with secret, reconnect after a simulated HA restart, same installation ID after a container restart, exit code 0 on stop, no ERROR or Traceback in the log. A run with no token stops the container with exit code 2 (finish script).
- mise run check passed (997 app tests, 36 + 8 integration tests, frontend 43 tests).
- Environment: integration/.venv held an incomplete HA Core install (41 components). uv sync --reinstall-package homeassistant fixed it. No repository change.

Open (user verifies on HA OS, README checklist): AC 1 (install from the local apps folder and start), AC 2 (integration copy and link), AC 4 (backup holds /data), task-6 AC 1 (Supervisor proxy and reconnect after an HA restart), config/auth/list with SUPERVISOR_TOKEN, auth/current_user matches the context user.

Proposed follow-ups (not created): show the integration set-up values (host, port, secret) on the Settings page, so that a manual set-up does not need the api/integration URL; publish a prebuilt image so that HA does not build the image.

Lead: merged into impl. The lead re-ran mise run smoke-app: passed. Status stays In Progress: the user chose to install by hand and will verify AC #1, #2 and #4 with the README checklist.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Packaged hagent as a local HA app: hagent/ (config.yaml, Dockerfile on base-debian trixie with uv CPython 3.14 and a Node build stage for the UI, s6 service, translations, docs, placeholder images), scripts/package-app.sh for the self-contained build folder, and README installation, first configuration, backup and verification sections. Verified with test_app_package.py (linter schema, Supervisor option rules, app config sections), spikes/task-21/smoke.sh (amd64 build, start under s6 against FakeHA, migrations, UI, API, link, HA restart, app restart, clean stop) and mise run check. AC 3 is checked. AC 1, 2 and 4 need a real HA OS host; the user verifies them with the README checklist.
<!-- SECTION:FINAL_SUMMARY:END -->
