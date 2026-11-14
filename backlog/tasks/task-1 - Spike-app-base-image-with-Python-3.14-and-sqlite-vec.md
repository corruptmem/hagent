---
id: TASK-1
title: 'Spike: app base image with Python 3.14 and sqlite-vec'
status: Done
assignee:
  - '@claude'
created_date: '2026-11-14 23:03'
updated_date: '2026-11-14 23:19'
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
- [x] #1 A minimal app image builds for amd64
- [x] #2 Inside the image, Python 3.14 runs and sqlite-vec loads into SQLite
- [x] #3 A vec0 table accepts inserts and returns KNN results in a smoke test
- [x] #4 FTS5 is available in the same SQLite build
- [x] #5 Findings, including the chosen base image and any build steps, are recorded in the task notes, and docs/design.md is corrected if an assumption was wrong
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Probe the host uv-managed CPython 3.14 (python-build-standalone) for load_extension, FTS5 and sqlite-vec.
2. Probe the HA Alpine base-python 3.14 image for the same facts, to record why it is rejected or kept.
3. Write spikes/task-1/Dockerfile on ghcr.io/home-assistant/base-debian (trixie, pinned) with uv-managed CPython 3.14 and pinned sqlite-vec.
4. Write spikes/task-1/smoke.py: assert Python 3.14, load sqlite-vec, vec0 insert plus KNN, FTS5 MATCH; exit non-zero on failure.
5. Build and run the image for amd64. Record the output.
6. Check docs/design.md sections 6, 13.3 and 22 for wrong assumptions. Correct only if wrong.
7. Record findings in notes. Run mise run check. Commit.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Scope change (from the lead, by user decision): the app will not run on Raspberry Pi or aarch64. AC #1 now reads 'A minimal app image builds for amd64'. The aarch64 wheel check is dropped.

## Findings

Chosen base image: ghcr.io/home-assistant/base-debian:trixie-2026.11.0 (Debian 13, glibc 2.41). The image keeps the HA s6-overlay /init entrypoint.

Python: uv 0.13.0 installs a uv-managed CPython 3.14.8 (python-build-standalone) into /opt/python. A venv at /opt/venv holds the packages. UV_PYTHON_PREFERENCE=only-managed blocks the Debian system Python (3.13). Its SQLite is 3.53.1, with extension loading and FTS5 turned on.

sqlite-vec: 0.1.9, pinned, from the PyPI manylinux_2_17 x86_64 wheel.

Rejected alternative: ghcr.io/home-assistant/amd64-base-python:3.14-alpine3.24-2026.11.0 (musl). Its Python 3.14.10 SQLite 3.53.4 has extension loading and FTS5. But 'pip install sqlite-vec==0.1.9' fails with 'No matching distribution found'. PyPI has no musllinux wheel and no sdist for 0.1.9. The HA musllinux wheel index has none either. Alpine needs a compile step from the sqlite-vec C source, so Debian is simpler.

Build steps (spikes/task-1/):
1. docker build --platform linux/amd64 -t hagent-spike-1 spikes/task-1
2. docker run --rm hagent-spike-1 (runs smoke.py under s6), or add --entrypoint python and pass /opt/spike/smoke.py.

Smoke output (amd64, under s6 /init, exit 0):
python: 3.14.8 (main, Oct  9 2026, 15:20:54) [Clang 22.1.3 ]
sqlite: 3.53.1
sqlite-vec: v0.1.9
vec0 knn: [(1, 0.05000000074505806), (3, 0.11180341988801956)]
fts5 match: [(1,)]
PASS

Image size: 397 MB (uncompressed).

docs/design.md: sections 6, 13.3 and 22 state no base-image or libc assumption. No correction is necessary.

Recommendation: use spikes/task-1/Dockerfile as the start of the real app Dockerfile. Keep ARG BUILD_FROM so HA app build tooling can set the base.

Proposed follow-ups (not created):
- Pin sqlite-vec==0.1.9 in app/pyproject.toml when the knowledge module starts.
- Optionally add a 'Base image' row to design section 22 (HA base-debian trixie plus uv-managed CPython 3.14), if the lead wants the decision in the design.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Built an amd64 spike image on the HA base-debian trixie image with a uv-managed CPython 3.14.8 and sqlite-vec 0.1.9 (spikes/task-1/). The smoke test proves Python 3.14, sqlite-vec load, vec0 insert plus KNN, and FTS5, and exits 0 under s6 /init. The HA Alpine Python image is rejected: no musl wheel or sdist exists for sqlite-vec. No design.md correction is necessary. AC #1 was narrowed to amd64 by user decision. mise run check passes. Commit b633d41.
<!-- SECTION:FINAL_SUMMARY:END -->
