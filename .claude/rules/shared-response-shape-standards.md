# Response Shape Standards

How an answer to the user is shaped. Partly adapted from the MIT-licensed `i-have-adhd` skill (ayghri/i-have-adhd).

## Scope

These rules govern what is written **to the user in a session**. They do not apply to documentation (README, `docs/`, ADRs, commit messages, issues) or to agent-to-agent communication: a sub-agent shares no context and needs background, constraints and the full finding list stated explicitly. Where a project has its own output requirements, the project wins.

## The answer comes first

- Command, path, diff or result in the first line; reasoning after it.
- The opening sentence states what holds, not what is about to happen.
- End either finished, or on exactly one concrete next action.
- A final report follows the order in `shared-working-standards.md` → *Verification and final report*.

## Carry the state

- In multi-step work, say each turn where it stands; the previous message is no longer on screen. A todo list that already does this is enough.
- Show finished work concretely: what is possible now that was not before.

## Errors stated plainly

Name cause, location and fix. No "there seems to be a problem".

## Leave out

- Announcements of what you are about to do
- Summaries of what just happened
- Closing offers ("let me know if …")
- Figurative phrasing where the literal action is shorter

## Limits of this shape

- **Completeness beats brevity.** Relevant items are ordered and grouped, never capped. What is left out is named as left out.
- **Uncertainty stays in.** A hedge that carries real doubt is content.
- **Time estimates need evidence** like any other claim; estimate only what was measured, and say so.
- **"Explain X" suspends the brevity, not the shape.**
