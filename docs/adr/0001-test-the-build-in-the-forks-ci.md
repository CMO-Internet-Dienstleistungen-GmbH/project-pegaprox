# ADR 0001: Test the rebuilt tree in the fork's CI, not on the sync host

- Status: accepted
- Date: 2026-10-08

## Context

`scripts/sync-upstream-release.sh` used to run the test suite in a local venv
on whatever machine ran the sync, and refused to tag unless it was green.

Upstream's suite assumes the environment of its own CI (`test.yml`,
`ubuntu-24.04`, Python 3.12, a non-root user). Starting with v1.3.0 it
contains tests that read `/proc/uptime`, run scripts under `dash` and rely on
Linux file semantics. On a macOS workstation 107 of them fail on the plain,
unpatched release tag — the same 107 that failed on the patched build. Earlier
syncs had already failed on the host for other environmental reasons, such as
the descriptor limit of a terminal-launched process. A red result on the host
therefore says nothing about the tree, and a sync that cannot go green blocks
every release.

A Linux container on the host was considered and rejected: it reproduces CI
only approximately (it ran as root, which made three upstream tests fail that
are green in upstream's CI) and it still ties the gate to one machine.

## Decision

The test gate runs upstream's own `test.yml`, unchanged, in the fork's GitHub
Actions:

- the release tag is force-pushed to `cmo/ci-base`, the built commit to
  `cmo/ci`;
- one standing draft pull request `cmo/ci` → `cmo/ci-base` makes upstream's
  `pull_request` trigger fire on every push to `cmo/ci`. Because the build
  descends from the release, the merge GitHub tests has exactly the built tree;
- the script waits for the run whose head is the built commit and stops on any
  result other than success. The tag message links the run.

The suite is no longer run on the sync host. `verify.ci` in `patches.yml` names
the workflow and both branches.

## Consequences

- The gate is the environment upstream itself tests in; a red run means the tree
  is red.
- No workflow file is added or changed — patches stay clear of
  `.github/workflows/`, and upstream changes to `test.yml` apply automatically.
- The sync needs the `gh` CLI with push rights to the fork. A sync without
  `--push` now pushes the two CI branches; `cmo/main` and the tag stay local.
- The CI branches are reused, never deleted — the repository's ruleset forbids
  deleting branches, so a branch per build would pile up.
- Actions minutes for standard runners are free for public repositories, so the
  billing concern that keeps the watcher out of Actions does not apply here. If
  the fork ever becomes private, the gate consumes the organisation's minutes.
- The routine that runs the sync in a cloud sandbox needs `gh` and network
  access to the GitHub API; without them the gate stops with an error instead of
  publishing.
