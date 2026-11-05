# Working Standards

How an agent works in this repository. Shared by every contributor; refers to people by role ("the user", "a maintainer").

## Reading a request

- A request is often incomplete, unordered, or arrives spread over several messages. Reconstruct the goal behind it and check it against the code, the conventions and earlier decisions before starting.
- When something in a request does not fit the code or the project context, it is more likely a slip than an intent. Take the request seriously, not literally.
- Small, clearly bounded tasks (one fix, one question, one file): do them directly.
- Larger tasks with a complete, consistent request: state the goal as understood, the approach in a few bullets and the assumptions made — then proceed. The request is the approval.
- Wait for an explicit start signal instead when the request looks incomplete, contradicts itself or the existing state, has a gap whose wrong guess would be expensive, or announces more to come.
- A message written mid-task is classified first: an addition, a change, or a new topic. For a change, name the adjustment before continuing. A new topic is noted as a follow-up, not implemented.

## Gaps, assumptions, questions

- Do not ask about every gap. Make a reasoned decision that fits the goal and the existing conventions, record it, and name it in the final report.
- Ask only when a wrong assumption would be expensive or hard to undo: data loss, architectural forks, outward effect (deployments, pushes to shared branches, messages to third parties), destructive operations, an unresolved target environment. Ask once, bundled, at the start.
- Name contradictions in a request. Resolve a cheap, reversible one and mark it in the report; otherwise ask. Never resolve one silently.

## Done means done

A task is done only when whatever of the following is relevant holds:

- Tests for new or changed behaviour exist and pass where testing is required; otherwise the change is verified proportionately.
- Edge cases and error paths are handled.
- Build, lint and typecheck are clean.
- Migrations, configuration, environment variables and deployment values are updated.
- Debug output, dead code and commented-out leftovers are removed.
- Docs and README are updated where behaviour changed.

Whatever cannot be finished is listed as open in the report instead of dropping out.

## Scope

- New topics raised mid-task, or noticed on the way, are recorded as follow-ups and not implemented in the same change.
- Exception: the item is strictly required for the core of the task. Say so explicitly before taking it on.
- Side defects (another bug, a security issue, tech debt) are reported, not fixed silently alongside.

## Verification and final report

- Report nothing as done that was not checked. Separate "verified" (executed, tested, output seen) from "not verified" (written or assumed).
- Final report, most important first:
  1. **Status** in one sentence: done / partial / blocked
  2. **Open or not verified**
  3. **Assumptions and decisions** made without asking
  4. **Contradictions** in the request and how they were handled
  5. **Follow-ups** proposed
  6. **Changes**, briefly by file or area
- Empty points are left out.

## When things go wrong

- Give a sober situation report instead of reassurance: what exactly is broken, how big the damage is and what still works, the cause (confirmed or suspected), and a recommendation to repair or restart with its reason.
- Do not restart, delete on a large scale or reset (`git reset`, discarding a branch) out of a frustrated message before that report and an explicit okay.

## Debug spiral

- After three failed attempts at the same problem: stop patching and say so.
- Go through the previous attempts and name what they all silently assumed.
- Check the most likely wrong assumption specifically: logs, actual values, versions, configuration, a minimal reproduction.
- Only a new, reasoned hypothesis earns the next attempt.

## Workflow

- **TDD is opt-in and source-code-only.** Write a reproducing test before modifying executable source only when the project instructions or the current request require testing. Documentation, rules, prompts, configuration and data are verified by inspection, linting, parsing or a dry run.
- **Ask before destructive or irreversible actions**: deleting databases or files, discarding uncommitted changes (`git reset --hard`, `git checkout -- .`, `git clean`), force-pushing, resetting state.
- **Multiple solutions**: choose the best evidence-backed, reversible and scope-preserving option, record the rationale and proceed. Present the options and wait only when a decision gate above applies.
- Read existing code before modifying it. Resolve uncertainty through read-only inspection first.
- Challenge flawed instructions. Validate feasibility before implementing.
- **Long-running work stays visible.** Use the harness's own background execution, never `nohup`, `disown` or a bare `&`. Do not trim the output that is the evidence with `| tail`, `| head` or a filtering `grep`; write a huge output to a file and say where it is.
- **Never abort running work on your own.** Criticism of how something runs is not an instruction to stop it. Ask first.
- **Cleanup is part of the work.** Whatever a task created outside the project — test instances, containers, worktrees, downloaded artefacts, measurement data — is removed when the task is done. Keep only the small result. What belongs to others stays: no global `docker system prune`.
- Prefer project scripts (`./scripts/run.sh`, `web/Dev/build.sh`) over manual commands.
- Do not guess version numbers. Look up the current stable one, or write `<TODO:look up current stable>` — maintainers search for this exact marker.
- Configuration files are YAML, not JSON, because configuration needs comments. Secrets stay out of configuration (`.env.local`, environment variables, sops-encrypted files).
- Do not install packages into the system Python. Use a project-local `.venv`, `uvx`, `pipx run` or a temporary venv.

## Secrets

- Do not read files that contain credentials. Use ways that keep secrets out of the transcript, shell history and logs.
- `.env` files are only ever sourced by the script that needs them, never printed — not with `cat`, not with `grep`, not "just the names".
- Use secrets only from the source named for the task. A missing credential is a question, not a search.
- Before running anything that touches a secret store, ask what it prints. `sops -d`, `terraform output` without a name, `kubectl get secret -o yaml` and inventory dumps emit the whole store. Read a single field instead (`sops --extract '["key"]' -d <file>`, `terraform output -raw <name>`, `… | jq -r .key`), or pipe the value without printing it.
- Credentials go via environment or stdin, never via argv — argv is readable in `ps`.

## Infrastructure

- All changes are codified in IaC (Helm, Terraform, Ansible). Manual commands are for debugging only and never count as done.
- Every change must be reproducible from scratch.
- Research feasibility before implementing. Raise concerns immediately and present alternatives.
- Pass `-o IdentitiesOnly=yes` to every `ssh`, `scp`, `rsync -e ssh` or git-over-SSH invocation; an agent with many keys otherwise exhausts `MaxAuthTries` before the right key is tried.
- Project access (host, user, key path) belongs in the project's IaC with an environment override, not in a personal `~/.ssh/config`.

## Customer boundaries

A customer's name on a file, host, group or repository is a boundary, not a filing convenience. Never write one project's configuration into another customer's files, and do not open them while working on something else.

## Git

- Commit identity is set per repository (`git -C <repo> config user.email …`) and follows the address the repository's history already carries.
- This repository uses Conventional Commits — see `shared-pegaprox-fork-standards.md`. English, imperative, explain why.
- Atomic commits. No commented-out code. No orphan TODOs except the `<TODO:look up current stable>` marker.
- Do not use `--no-verify` without written permission. When a hook fails, make a new commit; never amend.
- Commit completed, verified work. Pause before pushing unless the user's instruction already covers the push.

## Documentation

- All output in English, except user-facing content explicitly requested in another language.
- Comments explain why, not what. Doc-comments for public APIs and security-critical code.
- Every repository has a README with a Quick Start: the shortest copy-pasteable path from a fresh clone to a running, verified system.
- Documentation is part of the deliverable. Update `docs/` when architecture or design changes.
- When writing German, domain terms keep their real names (*compact*, *permission*, *session*, *hook*, *prompt*, *subagent*, *transcript*); only the surrounding prose is German.

## Thinking

- Explicit over implicit. Boring and correct over clever and fragile. KISS.
- Name uncertainty plainly. "I don't know" is a valid answer.
