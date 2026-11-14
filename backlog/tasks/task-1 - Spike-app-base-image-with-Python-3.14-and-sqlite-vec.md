---
id: TASK-1
title: 'Spike: app base image with Python 3.14 and sqlite-vec'
status: To Do
assignee: []
created_date: '2026-11-14 23:03'
labels:
  - spike
  - app
dependencies: []
references:
  - docs/design.md
priority: high
type: spike
ordinal: 1000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Prove that the hagent app container can run Python 3.14 with the sqlite-vec extension loaded. HA standard app base images may be Alpine (musl), and sqlite-vec publishes glibc wheels only. The result decides the base image for the app.

Design: docs/design.md sections 6, 13.3 and 22.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 A minimal app image builds for amd64 and aarch64
- [ ] #2 Inside the image, Python 3.14 runs and sqlite-vec loads into SQLite
- [ ] #3 A vec0 table accepts inserts and returns KNN results in a smoke test
- [ ] #4 FTS5 is available in the same SQLite build
- [ ] #5 Findings, including the chosen base image and any build steps, are recorded in the task notes, and docs/design.md is corrected if an assumption was wrong
<!-- AC:END -->
