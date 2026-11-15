---
id: TASK-11
title: 'Knowledge stores, document upload and hybrid search'
status: Done
assignee:
  - '@claude'
created_date: '2026-11-14 23:04'
updated_date: '2026-11-15 01:08'
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
- [x] #1 Stores can be created, described, size-limited, attached to several agents and shared
- [x] #2 knowledge.write uses version checks; a stale write fails; every change records its author and can be restored
- [x] #3 PDF (text layer), Markdown, plain text and HTML documents are extracted, split and indexed; results name the document and page
- [x] #4 knowledge.search, memory.search and ha.find_entities return hybrid-ranked results limited to the agent's access and scope
- [x] #5 Embeddings run in a background queue, deduplicated by content hash; a model change re-indexes
- [x] #6 Search falls back to keyword-only when sqlite-vec or the embedding provider is unavailable
- [x] #7 Each turn includes store descriptions, a name-sorted index (50 entries by default) and injected main files
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Add sqlite-vec==0.1.9 and pypdf to the app. Add a mypy override for sqlite_vec.
2. Migration 0500: extend knowledge_stores (size_limit, updated_at); add knowledge_files, knowledge_file_versions (author, tombstones), knowledge_documents, search_passages (collection, store_id, agent_id, source ref, location, content_hash, charge agent), an FTS5 index with triggers, search_embeddings (content_hash, model, dims, vector BLOB) and knowledge_settings. Every store reference uses ON DELETE CASCADE.
3. knowledge/vectors.py holds every sqlite-vec statement. The component loads the extension through db.run. Vector search uses vec_distance_cosine on BLOBs, filtered by scope before the limit (no vec0 KNN: it filters after the limit).
4. knowledge/index.py: passage index, FTS5 query sanitising, RRF merge, keyword-only fallback.
5. knowledge/embedder.py: background queue. Embed each content hash once per model. Charge agent-written text to the author and documents and entities to the installation. A model change re-queues every passage. Errors set a status reason and back off.
6. knowledge/extract.py: PDF (pypdf text layer), Markdown, text, HTML; split at headings, paragraphs and pages. Run in a thread.
7. knowledge/service.py: stores, files with versions and change history, restore, documents, size limits. It implements the KnowledgeStore protocol.
8. knowledge/context.py: turn context: store descriptions, name-sorted index (50 entries by default, with total), injected main files. Task-10 calls it.
9. knowledge/entities.py: entity catalogue index, rebuilt on registry reload. tools/: EntitySearch protocol on ToolEnv; ha.find_entities ranks only scope-filtered candidates. tools/: StoreError maps to an error_code.
10. knowledge/history.py: history and conversation index API for task-10, and search_history with filters.
11. knowledge/api.py: management routes under /api/knowledge (stores, files, history, restore, documents with streamed upload, search, settings, status). Document them in docs/api.md.
12. Register in default_components; tests with a fake embedding client, including scope-violation tests; app/README.md hand-off; mise run check.

Revision: files and documents share one table, knowledge_items, with knowledge_item_versions and knowledge_document_sections, because they share one path namespace. sqlite_vec uses a type: ignore at its import, not a mypy override. The vector search has a distance cutoff (max_vector_distance).

Security review fixes (branch task-11-fix):
M1. build_turn_context reads at most main_file_max_chars of each main file (SQL substr) and the full length. It clips the text and appends a note with the total and a pointer to knowledge.read. PromptBuilder passes turns.main_file_max_chars. The executor fallback clips too.
M2. One helper wraps each injected main file in a fenced data block with an attribution line (store, path, last author). The fence is longer than any backtick run in the text, so the text cannot close it. The stable knowledge header states that store text is data, not instructions. The recall section gets the same note.
L1. After each version insert, prune history per store: keep at most N versions per path, and keep non-current history bytes at or below the store size limit (oldest first).
L2. On BudgetExceededError with scope agent, the embedder skips that charge for the drain and continues with other charges. It retries after embed_retry_seconds and reports the skipped agents in the status.
L3. EntitySearch.rank_entities takes agent_id. FindEntities passes ctx.agent.id; the catalogue passes it as charge_agent_id.
Each review probe becomes a regression test.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Implementation (commits 20b2fa9, dc45cc8, 8c5f549):
- Package app/src/hagent_app/knowledge/. Migration 0500 extends knowledge_stores (size_limit, updated_at). It adds knowledge_items, knowledge_item_versions, knowledge_document_sections, search_passages with the FTS5 index search_fts and its triggers, search_embeddings and knowledge_settings. Every store reference uses ON DELETE CASCADE.
- Decision: vectors are float32 BLOBs in search_embeddings. A query uses vec_distance_cosine over the passages in scope. Reason: a vec0 KNN query applies its limit before other filters, so a scope filter can remove every result. A BLOB table also needs no fixed dimension, and a model change re-queues every passage. All sqlite-vec statements are in knowledge/vectors.py. The component loads sqlite-vec through db.run, so no migration needs it. sqlite-vec==0.1.9 and pypdf are pinned in app/pyproject.toml.
- Decision: the vector search drops matches above a cosine distance of 0.75 (option max_vector_distance). Without the cutoff, every passage or entity in scope ranks. The right value depends on the embedding model.
- Decision: query embeddings are charged to the installation, because the KnowledgeStore.search protocol has no agent ID. Text that an agent writes is charged to that agent. Documents and the entity catalogue are charged to the installation.
- tools/ edits (small, additive): StoreError in tools/stores.py; the executor maps it to error_code. EntitySearch protocol, plus a field on ToolEnv and on ToolsComponent. In FindEntities.authorize, the scope-filtered candidates go to EntitySearch for ranking; without it, or on an error, the tool uses the word match. The registry snapshot keeps entity aliases, device names and label names (new optional fields) for the entity catalogue.
- memory.search (agreed with the lead): HistoryIndex (knowledge/history.py) gives task-10 add/remove/search. No partial HistoryStore is wired into default_components. A test adapter proves hybrid ranking and agent scoping through the executor.
- AC7: build_turn_context and KnowledgeComponent.turn_context give the store list, the name-sorted index (option index_entries, 50 by default) and the injected main files. Task-10 must put this text into each turn. No turn exists yet, so AC7 stays unchecked.
- Management API: /api/knowledge/... (stores, items, files, history, restore, documents with a streamed upload above the aiohttp 1 MiB default, search, status, settings). See docs/api.md 'Knowledge stores'.
Evidence (mise run check: ruff, mypy strict, 416 pytest passed, frontend 20 passed):
- AC1: test_knowledge_stores.py::test_store_create_describe_limit_attach_and_share, test_size_limit_stops_writes_and_uploads.
- AC2: test_write_checks_versions_and_records_authors (stale write gives version_conflict; history lists each author; restore).
- AC3: test_documents_are_extracted_split_indexed_and_named (PDF page, Markdown, text, HTML; results name the path and page), test_knowledge_text.py.
- AC4: test_knowledge_search.py::test_knowledge_search_is_hybrid_and_limited_to_attached_stores, test_knowledge_tools_out_of_scope_never_reach_the_store, test_memory_search_is_hybrid_and_limited_to_the_agent, test_find_entities_ranks_only_entities_in_scope.
- AC5: test_embeddings_are_queued_deduplicated_and_charged (one embedding per hash, charges, model change re-index, prune).
- AC6: test_keyword_only_without_a_model, test_keyword_only_when_sqlite_vec_is_off, test_keyword_only_when_the_provider_fails, test_load_failure_gives_a_reason.
- AC7 (builder only): test_turn_context_lists_stores_index_and_main_files, test_turn_context_index_size_option.
Open: task-10 wires HistoryIndex into its HistoryStore and turn_context into the prompt. The status sensor (design 13.3) can read GET api/knowledge/status. Proposed follow-up (not created): give KnowledgeStore.search the agent ID, so that query embeddings count towards the agent budget.

Second round (lead request, merge commit 8c07289):
- Merged impl (task-10). Conflicts in app.py, app/pyproject.toml and test_http_server.py are resolved. The order is tools, knowledge, turns.
- turns.history.index = knowledge.history_index. HistoryIndex implements the protocol of turns/history.py (index_history, index_conversation, search_history). Migration 0500 (not merged yet) gets a source_key column, so the source filter matches the source or the source key, as in the fallback of task-10.
- turns.knowledge_context = knowledge.turn_context. PromptBuilder uses it for part 3 when it is set, with turns.index_max_entries as the index size. It also derives the main file requests from it. If the call fails, the builder falls back to the executor path. The knowledge option index_entries is removed, so that only one option sets the index size.
- KnowledgeStore.search now takes agent_id (tools/stores.py, store_tools.py, FakeKnowledge, a fake in test_turns.py). Query embeddings of knowledge.search and memory.search count towards the invocation agent. Searches from the management API count towards the installation.
- Fixed an embedder race: a drain that started before a wake no longer reports idle.
Evidence (mise run check: 447 pytest passed, frontend 20 passed):
- AC7: test_knowledge_turns.py::test_each_turn_includes_stores_index_and_main_files. A real TurnRunner turn has the store list, a 50-entry name-sorted index with the total, the injected private MAIN.md (no shared one), the design 9.5 order, identical instructions across turns, and a main file request. Also test_turn_index_size_comes_from_the_turns_option and test_knowledge_off_leaves_the_stores_out.
- memory.search end to end: test_memory_search_finds_indexed_turns_through_the_executor. TurnRunner turns are indexed (not dry runs). memory.search through the executor finds the agent's turn by vector, never another agent's turn, and charges the query to the agent.
- Agent charge: test_knowledge_search_charges_the_query_to_the_agent. Wiring: test_default_components_wire_the_knowledge_into_turns.

Security review fixes (commit e7e2562, branch task-11-fix):
- M1: build_turn_context takes main_file_max_chars. KnowledgeService.main_files reads only that part of each MAIN.md (SQL substr) and returns the full length and the last author (MainFile). PromptBuilder passes turns.main_file_max_chars. KnowledgeContextSource is now a Protocol with three arguments. The main file request uses the full length. The executor fallback clips too.
- M2: data_block in knowledge/context.py wraps each main file in a fenced block. A line before it names the store, the path and the last author (agent ID, or the user). The fence has more backticks than the longest run in the text. The clip note is outside the fence. DATA_NOTE, a fixed text, opens the knowledge part and the recall part. It states that store text is data, not instructions. The note holds no variable value, so the prefix stays the same across turns.
- L1: _prune_history runs in the same transaction as each file write and delete. It keeps the newest version of each path, at most MAX_FILE_VERSIONS (50) versions per path, and the text of earlier versions at or below the store size limit. Oldest first. docs/api.md states the retention.
- L2: the embedder catches BudgetExceededError with scope agent, skips that charge for the drain (SQL HAVING ... NOT IN json_each), and continues. It retries after embed_retry_seconds with a timer, and lists the agents in status budget_skipped_agents. last_error and search_mode do not change. An installation budget error stops the drain as before.
- L3: EntitySearch.rank_entities takes agent_id. FindEntities passes ctx.agent.id. EntityCatalog passes it as charge_agent_id.
Evidence: mise run check passes (ruff, mypy strict, 588 pytest passed, frontend 20 passed). test_knowledge_security.py turns probes P1 to P6 into 8 tests. 7 of them fail on the code before the fix. The P4 test (scope checks and FTS5 syntax) passes before and after, because it guards behaviour that was already sound.

Review fix: DATA_NOTE no longer says that only the '# Instructions' section gives instructions. The runtime also instructs the agent in other sections, for example the main file maintenance request. It now says that instructions come only from the runtime sections of the prompt.
Proposed follow-ups (not created): (1) L2: _next_batch uses MIN(charge_agent_id), which ignores NULL. If a hash is shared by an over-budget agent and installation text, the embedder skips the whole hash until the budget allows it. (2) L1: _prune_history scans the version sizes of the whole store on each write. This is cheap at the 1 MB default limit and slow near the 1 GB maximum. A running total per store would fix it.

Lead: an independent review found 0 high, 2 medium and 3 low issues; task-11-fix fixed all five with regression tests. Minor follow-ups (shared-hash budget skip edge case; version prune scan cost near 1 GB) are listed for the user.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Built the knowledge package: knowledge stores (create, describe, size limit, shared attachments), files with version checks and a change history with authors and restore, document upload (PDF text layer, Markdown, text, HTML), and hybrid search (FTS5 plus sqlite-vec BLOB vectors, merged with RRF, with a distance cutoff and a keyword-only fallback). Also a background embedding queue (one embedding per content hash and model, a re-index on a model change), the HA entity catalogue for ha.find_entities, and the /api/knowledge management routes. After the task-10 merge, each agent turn includes the knowledge context from KnowledgeComponent.turn_context, the turn history indexes turns for memory.search, and query embeddings count towards the invocation agent. Security review fixes: injected main files are clipped to turns.main_file_max_chars and go into fenced data blocks with an attribution line and a fixed data-not-instructions note; the version history is bounded by count and by the store size limit; an over-budget agent no longer stops the embedding queue; ha.find_entities query embeddings count towards the agent. Verified with mise run check (588 Python tests, front-end checks) and test_knowledge_security.py (8 tests from the review probes; 7 fail before the fix). AC1 to AC7 are checked.
<!-- SECTION:FINAL_SUMMARY:END -->
