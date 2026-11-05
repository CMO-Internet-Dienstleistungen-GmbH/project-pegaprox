# Shared Knowledge Index

`.claude/knowledge/` holds a reviewed snapshot of the maintainers' technology knowledge: gotchas, measured facts and standards that outlive a single task. The files are not loaded automatically, because together they are far too large for every session.

**Before working in an area listed below, read the matching file.** Match by the technology or by the file patterns in the table. `development-foundations`, `future-proof-code` and `security-standards` apply to every code change.

| Topic | What it covers | Applies to |
|---|---|---|
| [`bash-standards`](../knowledge/bash-standards.md) | Bash strict-mode pitfalls, POSIX compatibility, cross-platform gotchas, and coding patterns | `**/*.sh`, `**/Makefile`, `**/Justfile` |
| [`browser-automation`](../knowledge/browser-automation.md) | Browser automation gotchas: HAR capture, fetch interception, Cloudflare bypass | `**/*.har` |
| [`claude-code-internals`](../knowledge/claude-code-internals.md) | Claude Code platform behavior, hooks, permissions, MCP registration, and session management | `**/.claude/**`, `**/CLAUDE.md`, `**/.claude.json` |
| [`claude-skill-authoring`](../knowledge/claude-skill-authoring.md) | Claude Code skill authoring best practices, structure, patterns, and anti-patterns from official Anthropic guides | `**/skills/**/SKILL.md`, `**/.claude/skills/**`, `**/commands/**` |
| [`data-migration-gotchas`](../knowledge/data-migration-gotchas.md) | Pitfalls when converting data between formats (SQL dumps, YAML, JSON, CSV) for infrastructure-as-code migrations | `**/scripts/**`, `**/convert*.py`, `**/migrate*.py`, `**/data/**` |
| [`development-foundations`](../knowledge/development-foundations.md) | Universal code quality, architecture (SOLID), error handling, and logging standards — language-agnostic | `**/*` |
| [`documentation-standards`](../knowledge/documentation-standards.md) | Documentation standards: README structure, docs/ organization, ADRs, code comments, and writing style | `**/README.md`, `**/docs/**`, `**/CLAUDE.md` |
| [`future-proof-code`](../knowledge/future-proof-code.md) | Measurable rules for clean, maintainable, future-proof code: size limits, expression complexity, comments that explain intent, no literal design values, test naming, and the review checklist that enforces them | `**/*` |
| [`hyperv-to-proxmox-migration`](../knowledge/hyperv-to-proxmox-migration.md) | Moving Windows guests from Hyper-V to Proxmox VE: virtio-win driver versions per guest OS, what Proxmox refuses in a VM create call, and the operating rules this migration path is built on | `**/hyperv_*.py`, `**/v2p.py`, `**/xhm.py` |
| [`projektplanung-tickets`](../knowledge/projektplanung-tickets.md) | Manual für Projektplanung und Ticket-Qualität: Epics, Stories, vertikaler Schnitt (INVEST/SPIDR), Definition of Ready/Done, Board-Workflow (Kanban, WIP-Limits), Entscheidungs-Management, Regeln für Agent-getriebene Abarbeitung (Claude Code + Codex-Review) | `**/docs/lessons.md`, `**/CLAUDE.md` |
| [`proxmox`](../knowledge/proxmox.md) | Proxmox VE platform gotchas: API, Packer templates, bpg/proxmox Terraform provider, KubeOne integration | `**/packer/**`, `**/*.pkr.hcl`, `**/proxmox/**` |
| [`security-standards`](../knowledge/security-standards.md) | Security-by-design standards: input validation, auth, secrets management, and OWASP prevention | `**/*` |
| [`testing-foundations`](../knowledge/testing-foundations.md) | Universal testing principles (TDD, Arrange-Act-Assert, mocking) — complements technology-specific testing knowledge | `**/*Test*`, `**/*test*`, `**/*spec*`, `**/*_test.*` |

## Keeping the snapshot clean

The repository is public. A finding added here follows the same rules as everything else committed: no hostnames, node or cluster names, addresses other than `127.0.0.1`, account names or customer names — keep the measurements, replace the names with placeholders (see `CLAUDE.md` → *Everything that leaves this repository is public*). Run the anonymisation grep from `CLAUDE.md` over `.claude/` before committing.
