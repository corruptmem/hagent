---
id: TASK-18
title: 'Management UI: shell, Ingress serving and agent screens'
status: To Do
assignee: []
created_date: '2026-11-14 23:04'
labels:
  - ui
dependencies:
  - TASK-7
references:
  - docs/design.md
priority: medium
type: feature
ordinal: 18000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Create the React single-page application (TanStack Router, Query and Table, built with Vite), served by the app through Ingress, with the agent definition screens. Set up front-end linting, formatting and type checking in pre-commit and mise.

Design: docs/design.md sections 6.1, 7.1, 7.2 (Agents) and 22 (Management UI stack).
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 The UI loads through Ingress with relative URLs and the X-Ingress-Path base path
- [ ] #2 The app accepts UI connections only from 172.30.32.2
- [ ] #3 All definition fields in section 7.1 can be edited, with entity pickers from live HA data
- [ ] #4 Definition versions can be compared and restored
- [ ] #5 A test invocation runs with sample data without changes to HA
- [ ] #6 Front-end lint, format and type checks run in pre-commit and mise run check
<!-- AC:END -->
