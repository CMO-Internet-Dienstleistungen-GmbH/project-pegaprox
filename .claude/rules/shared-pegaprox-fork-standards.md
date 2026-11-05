# PegaProx Fork Standards

`CMO-Internet-Dienstleistungen-GmbH/project-pegaprox` is a fork of `PegaProx/project-pegaprox`. Its branches do not mean what branch names usually mean, and getting that wrong destroys work silently.

## `cmo/main` is rebuilt, not extended

`cmo/main` is the default branch and the one everything runs from — upstream plus every CMO change. It is **regenerated at each release** by rebasing the feature branches onto the new upstream state.

**A commit made directly on `cmo/main` is gone at the next release, without warning and without recovery.** This has already happened once: an issue-templates commit pushed straight onto `cmo/main` disappeared within the hour when the branch was rebuilt beneath it.

So, without exception:

- **Every change to `cmo/main` lives on a feature branch first.** `feat/…`, `fix/…`, `chore/…`, matching the type its commits carry.
- **That branch is never deleted after merging.** It is the source `cmo/main` is rebuilt from.
- **It is rebased onto the new upstream at every release**, along with the others. A branch nobody rebases quietly stops being part of the product.

`main` is a plain mirror of upstream, held level by an automated fork-sync job via the `merge-upstream` API. Nothing of ours belongs there. Upstream work reaches `cmo/main` through the release rebuild — never a pull request from `main`.

## A patch is a branch, an issue, and tests

- **A branch**, never deleted, rebased at every release. A ruleset without bypass refuses its deletion.
- **An issue in the fork**, named in the patch's `requested_by`. It states the scope, what the patch deliberately does *not* cover, and checkable acceptance criteria. A conflict is resolved against it; without it, whoever carries the patch across a moved upstream is guessing.
- **Unit and integration tests** for what the patch does — tests that fail when this patch's behaviour is lost. A green suite alone only says nothing else broke.

## An issue number means the fork's issue

"Issue #<n>" always means the issue in the fork, never the upstream issue with the same number. The numbering overlaps and the two are unrelated. Upstream is in scope only when the request names it explicitly.

## What a rebase may not turn into

- No refactoring — not the conflicted file, not what was read on the way there.
- No change of scope. The patch does what its issue says.
- Nothing outside the patch. A bug spotted in code the patch merely touches is not fixed here.

Recommending any of it is welcome and becomes an issue in the fork. Acting on it in the same run does not happen.

## The commit convention is Conventional Commits

`type(scope): imperative summary`, lower case, no full stop. Types in use: `feat`, `fix`, `chore`, `ci`, `security`, `release`. Read the last twenty subjects before writing one.

One change is one commit. A commit that undoes an earlier one in the same session means both should have been squashed before pushing.

## Several sessions share a checkout

A checkout of this repository may be worked on by more than one agent at a time, and its branches are force-pushed. Before pushing, fetch and check the branch has not moved; push rewrites with `--force-with-lease=<branch>:<sha>`. `config/`, `logs/` and `web/` in a working checkout are untracked local artefacts and are never removed.

## The fork is public

No hostnames, instance URLs, addresses or infrastructure details in anything committed — including issue templates, workflow files and commit messages. See `CLAUDE.md` → *Everything that leaves this repository is public*.

## The assistant runs the sync

`./scripts/run.sh sync`, `publish` and `verify` are run by the assistant when a maintainer asks for a rebuild or a release. A request for a rebuild is a request to run it, and a request to publish is the authorization for the pushes that workflow needs.

Background: a `publish` started from Terminal.app once died in the test suite with `OSError: [Errno 24] Too many open files`, while the same tree passed in the assistant's shell. macOS gives a terminal-launched process 256 descriptors (`launchctl limit maxfiles`); the suite spikes to ~360 for a few seconds. The script now raises its own limit, but the episode is why the run belongs in one place.

Before any rebuild: check that no worktree has `cmo/main` checked out. The script refuses to rebuild a branch that is, and a leftover worktree on it blocks a release.
