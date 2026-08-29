# CMO fork automation — PegaProx

This branch (`cmo/automation`) holds **only** the tooling that keeps our fork
of [PegaProx](https://github.com/PegaProx/project-pegaprox) in sync with
upstream releases. It carries no application code on purpose: it can never
conflict with an upstream release, and the integration branch stays exactly
"upstream release + our patches".

## Quick Start

From a clone of the fork, with `cmo/automation` checked out:

```bash
git clone git@github.com:CMO-Internet-Dienstleistungen-GmbH/project-pegaprox.git
cd project-pegaprox
git checkout cmo/automation

# Is there a new upstream release we have no tag for?
./scripts/run.sh check          # exit 0 = nothing to do, 10 = sync needed

# Rebuild locally and run the tests — publishes nothing
./scripts/run.sh sync

# Same, but push the branch and the new tag once the tests are green
./scripts/run.sh publish

# Ask the fork what is actually published right now
./scripts/run.sh verify
```

Needs `git`, `python3` (with PyYAML), `curl`, and `node`/`npm` for the
frontend bundle. `GITHUB_TOKEN` is optional and only lifts the anonymous API
rate limit — pushing uses whatever credentials git is configured with.

## What it produces

| ref | meaning |
|---|---|
| `cmo/main` | current integration state. **Rebuilt and force-pushed** on every sync — never merge into it. |
| `v<release>-cmo.<n>` | immutable tag, e.g. `v1.0.2-cmo.1`. This is what you deploy and roll back to. |

`<n>` counts up within one upstream release: `v1.0.2-cmo.2` means the same
upstream release with a changed patch set (a fix added, reworked, or dropped).

## How a sync works

1. Ask the GitHub API for the latest upstream **release** (not just any tag).
2. Start a throwaway worktree at that release tag.
3. Replay every patch from `patches.yml` in order. For each one:
   - if its upstream PR is **merged**, drop it — the fix is in the release now;
   - if a commit is already contained, skip it;
   - if a conflict is limited to a **generated file** (`web/index.html`),
     regenerate it with `web/Dev/build.sh` and continue;
   - any other conflict stops the run and asks for a human.
4. Regenerate the artefacts once more and commit them if they changed.
5. Run the test suite in an isolated venv, exactly like the upstream CI does.
6. Compare the result against the newest existing tag. Identical tree → nothing
   to publish. Otherwise move `cmo/main` and create the next tag.
7. With `--push`: force-push the branch, push the tag.

**A tag is only ever created after a green test run.** `--skip-tests` refuses
to run together with `--push`.

## Adding, changing or retiring a patch

Everything lives in [`patches.yml`](patches.yml) — the scripts hardcode nothing:

```yaml
patches:
  - name: some-fix
    branch: fix/some-fix        # branch in the fork holding the commits
    base: Testing               # upstream branch it was built on
    upstream_pr: 812            # drops the patch automatically once merged
    summary: one line for the tag message
```

The commits taken are `merge-base(upstream/<base>, origin/<branch>)..<branch>`,
so rebasing the fix branch onto a newer upstream is transparent here.

When a fix lands upstream, you do not have to do anything: the next sync sees
the merged PR and drops it. Deleting the entry afterwards keeps the file tidy.

## The Claude Code cloud routine

A scheduled routine runs `check` daily and, when a new upstream release
appears, `publish`. It reports what it did and stops without publishing when a
conflict needs a human or the tests go red. Its prompt lives in
[`routine-prompt.md`](routine-prompt.md) — that file is the source of truth for
what the routine is told to do; edit it there and update the routine.

## Current patch set

See `patches.yml`. As of the first sync:

- **vnc-console-under-gevent** — the web console never connected under gevent
  on Python 3.13+ (upstream PR #741)
- **taskbar-remount-expand** — the task bar popped open on page load and on
  every cluster switch (upstream PR #739)
- **corporate-theme-source-of-truth** — "Corporate Light" from the settings was
  overridden on reload (upstream issue #742)
