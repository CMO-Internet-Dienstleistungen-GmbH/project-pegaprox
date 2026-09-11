---
name: pegaprox-fork-sync
description: Rebuild or publish the CMO PegaProx fork from patches.yml. Use when adding, updating, reordering, or retiring patches; moving work onto cmo/main; or producing a versioned CMO release tag. Not for ordinary upstream application development without fork integration.
---

# PegaProx Fork Sync

Maintain `cmo/main` as a reproducible integration branch: the current upstream
release plus the ordered patch set in `patches.yml`.

## Orient Before Changing State

1. Verify the repository root, current branch, worktrees, remotes and dirty
   state. Preserve unrelated work and the untracked `config/`, `logs/` and
   `web/` runtime artefacts.
2. Read `CLAUDE.md`, `README.md`, `patches.yml` and the relevant patch issue.
   Read `routine-prompt.md` as well when changing the autonomous rebuild.
3. Fetch before reasoning about remote branches, releases or publication.
   Every SSH Git command uses `GIT_SSH_COMMAND='ssh -o IdentitiesOnly=yes'`.

## Patch Contract

- Every patch has a long-lived feature branch, a fork issue in `requested_by`,
  regression tests and one entry in `patches.yml`.
- `base` names the upstream branch the patch branch is actually based on. After
  rebasing a patch from `main` to `Testing`, update `base` in the same automation
  change; otherwise unrelated upstream commits enter the patch range.
- `commit_message` is required and follows
  `<type>(<area>): <what changed>`. It describes the whole patch and is the
  stable subject of its squash commit on `cmo/main`.
- Do not derive a squash subject from the first or last development commit.
  Change `commit_message` only when the patch scope changed or the existing
  subject is materially wrong. Explain the reason in the `cmo/automation`
  commit body.
- Keep `summary` separate: it describes the patch in annotated tag output;
  `commit_message` identifies the Git commit.

## Rebuild

1. Finish and verify the patch on its feature branch first. Never edit
   `cmo/main` directly and never delete a patch branch after integration.
2. Commit `patches.yml`, script and documentation changes atomically on
   `cmo/automation` before rebuilding.
3. Use `./scripts/run.sh sync` for a local rebuild or
   `./scripts/run.sh publish` only with explicit publication authorization.
   Do not gate a changed patch set on `check`; a full run must compare trees.
4. The script applies patches in YAML order, squashes each under its configured
   `commit_message`, rebuilds `web/index.html`, runs the full suite, moves
   `cmo/main`, and creates the next local tag. With `publish`, it then
   force-pushes the rebuilt branch and pushes the immutable tag.

## Conflicts and Generated Output

- Resolve a conflict against the fork issue without refactoring, changing
  scope, or fixing adjacent defects.
- Resolve `web/index.html` only by running `web/Dev/build.sh`; never merge or
  hand-edit the generated bundle.
- A conflict outside configured generated files stops the automated rebuild.
  Repair the feature branch, verify it, and restart from the top.

## Verification and Publication

- Validate YAML parsing, shell syntax, ShellCheck and the configured
  `commit_message` format before committing automation changes.
- A completed rebuild requires the full test suite and a log check proving one
  squash commit per applied patch with the exact configured subjects.
- Before every push, fetch the exact branch and compare its remote tip. Use an
  explicit `--force-with-lease=<branch>:<recorded-sha>` for rewritten patch
  branches. Do not publish without authorization.
- After publication, run `./scripts/run.sh verify` and require the remote
  `cmo/main` SHA to equal the newest immutable tag.
