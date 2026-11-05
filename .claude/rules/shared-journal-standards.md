# Journal and Instruction-File Standards

## Working notes never enter git

Session journals and task notes (`.claude/task-notes/` and similar) are working memory for whoever is at the keyboard — dated, chatty, written for one reader. They stay git-ignored from the first entry:

- **They leak context.** Entries name hosts, networks, customers and the reasoning behind them. This repository is public; a journal entry has already carried a customer's topology into a public fork once.
- **They are not documentation.** What survives belongs in `docs/`, an ADR, `CLAUDE.md` or the shared knowledge under `.claude/knowledge/` — undated, addressed to a reader who was not there.
- **They pollute history.**

Already tracked? `git rm -r --cached <dir>` plus the ignore rule.

## README.md, CLAUDE.md and AGENTS.md are never a journal

- They hold only stable facts, rules and important information, in undated present tense.
- No progress notes, changelogs, session summaries, "current state", "in progress" or "next steps" sections.
- Next steps go to the issue tracker or the project board.

## Only written things exist

What is not written down does not survive a context compaction or a session end. Write decisions, findings and open points down when they arise — in the issue, `docs/`, an ADR, or the task notes — not later.
