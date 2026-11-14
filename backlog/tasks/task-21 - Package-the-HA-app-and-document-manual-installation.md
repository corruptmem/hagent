---
id: TASK-21
title: Package the HA app and document manual installation
status: To Do
assignee: []
created_date: '2026-11-14 23:04'
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
- [ ] #3 README documents both installation steps and first configuration
- [ ] #4 App data, including SQLite, is included in HA backups
<!-- AC:END -->
