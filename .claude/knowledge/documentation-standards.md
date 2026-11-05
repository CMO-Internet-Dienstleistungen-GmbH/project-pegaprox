---
category: Foundations
complements:
    - development-foundations
description: 'Documentation standards: README structure, docs/ organization, ADRs, code comments, and writing style'
file_patterns:
    - '**/README.md'
    - '**/docs/**'
    - '**/CLAUDE.md'
last_updated: "2026-09-29"
subcategory: Documentation
technologies:
    - documentation
    - markdown
    - readme
topic: documentation-standards
---
# Documentation Standards

## README.md (Required for Every Project)

Every project must have a `README.md` with these sections (in order):

1. **Title + one-liner** — what it is, in one sentence.
2. **Getting Started** — quickstart for new developers. Minimal steps to clone, install, run.
3. **Overview** — what the project does, who it's for, how it fits into the larger system. No directory trees.
4. **Setup** — detailed setup beyond getting started: config files, environment variables, external dependencies.
5. **Architecture** — high-level structure, how pieces connect. No directory tree listings — describe relationships and data flow instead.
6. **Stack / Versions** — pinned versions of major dependencies (OS, runtime, frameworks).
7. **Security** — auth model, secrets handling, hardening steps (if applicable).

Omit sections that genuinely don't apply — don't add empty placeholders. Never include directory tree listings (`tree` output) — they go stale instantly and add no value beyond what the repo itself shows.

## docs/ Directory

- Use `docs/` for content that goes beyond README scope: ADRs, architecture deep-dives, API contracts, guides, runbooks.
- One topic per file. Filenames: `kebab-case.md`.
- Keep docs close to what they describe — if a subdirectory has its own complexity, a local README.md is fine.

## ADR Format (Architecture Decision Records)

ADRs live in `docs/adr/` with numbered filenames: `0001-use-postgres.md`.

Required sections:
1. **Title** — short, descriptive (e.g., "Use PostgreSQL for persistence").
2. **Status** — Proposed | Accepted | Deprecated | Superseded by ADR-NNNN.
3. **Context** — what situation or problem prompted this decision.
4. **Decision** — what was decided and why. Include rejected alternatives with reasons.
5. **Consequences** — what follows from this decision (positive and negative).

## CLAUDE.md vs docs/

- **CLAUDE.md**: Rules, constraints, project state, next steps. Prescriptive ("do this", "never that").
- **docs/**: Descriptive content — architecture explanations, API documentation, how-to guides.
- Never duplicate between the two. CLAUDE.md references docs/ when detail is needed.

## Code Comments

- Comments explain **why**, not **what**. The code itself should be readable enough to explain what.
- Doc-comments for public APIs and security-critical code.
- No commented-out code. No orphan TODOs without issue reference.
- Do not add comments, docstrings, or type annotations to code you didn't change.

## Writing Style

- All documentation in English. Exception: user-facing content explicitly requested in another language.
- Direct, concise, imperative. "Run `./build.sh`" not "You can run the build script by executing...".
- Use tables for structured comparisons. Use code blocks for commands and config snippets.
- No marketing language, no filler words, no "simply" or "just".
- Keep lines readable — no single-sentence paragraphs stretched across 200 characters.

## Diagrams

- PlantUML is the default in Gradle/JVM projects (build-integrated rendering).
  Mermaid is acceptable where the target renders it natively (GitLab/GitHub
  Markdown) — see technology-assessments. One format per repository.
- Store diagram source in `docs/` alongside the documentation that references them.
- Diagrams must have a text description or caption — never rely solely on the visual.

## Changelog

- Use [Keep a Changelog](https://keepachangelog.com/) format.
- Categories: Added, Changed, Deprecated, Removed, Fixed, Security.
- Entries are human-readable summaries, not commit messages. Link to issues/MRs where applicable.
- Unreleased section at top for in-progress changes.

## Maintenance

- Documentation is part of the deliverable. Update docs when architecture or design changes.
- Stale docs are worse than no docs. Delete what's no longer true rather than leaving it.
- README must stay in sync with actual project state (commands, structure, versions).

## Promoted (2026-08-15)

- [PREFERENCE:0.80] User stories for Nextcloud Deck cards follow INVEST: each card must deliver standalone stakeholder value, not wrap a technical task; format is User-Story → Description → Acceptance Criteria → Out of Scope → Technical Notes → Open Questions.
- [CONVENTION:0.85] README files must describe the full setup path from an empty instance to a running system with placeholder values (e.g. `<nextcloud-host>`) — no hardcoded service names, cluster-local URLs, workstation paths, or deployment status entries belong there.

## Markdown line wrapping (2026-09-29)

- [PREFERENCE:0.90] Do not hard-wrap Markdown source to a fixed column width (no 80-column reflow). Write one physical line per paragraph and one per list item; let the renderer/editor soft-wrap. This applies to all generated Markdown — docs, reports, and chat output alike. Rationale: hard wraps produce noisy diffs, break when text is edited, and are what the maintainers explicitly asked to stop.
- This refines the older "no single-sentence paragraphs stretched across 200 characters" note: that was about *prose length* (don't pad one sentence into a giant paragraph), not about inserting hard line breaks. Long single-line paragraphs are correct; artificially wrapped ones are not.
