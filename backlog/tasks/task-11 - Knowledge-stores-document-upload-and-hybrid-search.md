---
id: TASK-11
title: 'Knowledge stores, document upload and hybrid search'
status: To Do
assignee: []
created_date: '2026-11-14 23:04'
labels:
  - app
  - knowledge
dependencies:
  - TASK-6
  - TASK-8
  - TASK-9
references:
  - docs/design.md
priority: high
type: feature
ordinal: 11000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Build knowledge stores with descriptions, files, uploaded documents, attachments with access levels, versioned files with change history, the knowledge tools, and hybrid search (FTS5 plus sqlite-vec, merged with RRF) over stores, history, conversations and the scope-filtered HA entity catalogue.

Design: docs/design.md sections 13, 13.3 and 13.4.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Stores can be created, described, size-limited, attached to several agents and shared
- [ ] #2 knowledge.write uses version checks; a stale write fails; every change records its author and can be restored
- [ ] #3 PDF (text layer), Markdown, plain text and HTML documents are extracted, split and indexed; results name the document and page
- [ ] #4 knowledge.search, memory.search and ha.find_entities return hybrid-ranked results limited to the agent's access and scope
- [ ] #5 Embeddings run in a background queue, deduplicated by content hash; a model change re-indexes
- [ ] #6 Search falls back to keyword-only when sqlite-vec or the embedding provider is unavailable
- [ ] #7 Each turn includes store descriptions, a name-sorted index (50 entries by default) and injected main files
<!-- AC:END -->
