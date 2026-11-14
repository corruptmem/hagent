# Adopt architecture decision records

## Context and Problem Statement

Code shows what a system does. It does not show why. Without a record, a new
contributor cannot tell if a pattern is deliberate. A contributor can be a
person or a coding agent. They also cannot tell if a past decision still
applies.

hagent needs a small, version-controlled record of decisions next to the code.

## Decision

Use architecture decision records (ADRs) in `contributing/decisions/`. An ADR
is a short document that states one decision, its reasons, and the rejected
alternatives.

- Write one ADR per file. Name it `YYYY-MM-DD-title-with-dashes.md`.
- Use these sections: Context and Problem Statement, Decision, Alternatives
  Considered, and optional More Information.
- Do not add YAML frontmatter or a status field. A merged pull request accepts
  the ADR.
- When a new ADR replaces an old one, link both records.
- Keep implementation plans and verification in issues and pull requests.

Accepted cost: each ADR takes time to write. Old ADRs need review so that
readers can see which ones no longer apply.

## Alternatives Considered

- No records: decisions stay in conversations and code comments. Rejected.
  The reasons get lost, and people discuss settled questions again.
- Wiki pages: decisions live outside the repository. Rejected. They drift
  from the code and have no version history with it.
- Request for comments (RFC) process: formal review cycles. Rejected. It is
  too heavy for a single-owner project.

## More Information

- Michael Nygard, "Documenting Architecture Decisions":
  <https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions>
