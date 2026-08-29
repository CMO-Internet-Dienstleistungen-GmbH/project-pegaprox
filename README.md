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
frontend bundle. No GitHub token and no API access: releases are discovered
with `git ls-remote`, which works wherever a clone works — sandboxed runners
answer 403 for `api.github.com` on repositories they have not attached.
Pushing uses whatever credentials git is configured with.

## What it produces

| ref | meaning |
|---|---|
| `cmo/main` | current integration state. **Rebuilt and force-pushed** on every sync — never merge into it. |
| `v<release>-cmo.<n>` | immutable tag, e.g. `v1.0.2-cmo.1`. This is what you deploy and roll back to. |

`<n>` counts up within one upstream release: `v1.0.2-cmo.2` means the same
upstream release with a changed patch set (a fix added, reworked, or dropped).

## How a sync works

1. Find the highest upstream release tag with `git ls-remote` (`vMAJOR.MINOR…`;
   anything with a suffix is ignored, so neither a release candidate nor our
   own `-cmo.n` tags can be mistaken for one).
2. Start a throwaway worktree at that release tag.
3. Replay every patch from `patches.yml` in order. For each one:
   - if the release already contains the work, drop it. `git cherry` compares
     patch-ids, so this holds even when the maintainer rebased our commits.
     The `upstream_pr` lookup adds the squash-merge case on top, and is
     skipped silently when the API is unreachable;
   - if a single commit is already contained, skip it;
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

## Triggering it

The work splits in two on purpose:

| half | what it does | cost |
|---|---|---|
| **notice** — `scripts/watch-upstream.sh` | compares the highest upstream release tag against the last one seen; fires a webhook when it changed | a `git ls-remote` — run it every 15 minutes if you like |
| **act** — the Claude Code routine | rebuilds, replays, tests, tags, and judges anything that went wrong | one cloud session, only when there really is a new release |

Nothing polls GitHub with a language model. `watch-upstream.sh` is a string
comparison; the routine only starts when that comparison changed.

### The watcher

```bash
WEBHOOK_URL='https://…' ./scripts/watch-upstream.sh          # cron / systemd timer
./scripts/watch-upstream.sh --dry-run                        # see what it would do
```

The first run only records the current release — setting it up never triggers
a rebuild of something that is already built. State goes to
`~/.local/state/pegaprox-upstream-release`; the webhook URL comes from the
environment and must stay out of the repository and out of `ps` (it is a
capability: whoever holds it can start the routine).

If a webhook call fails, the state is **not** advanced, so the next tick
retries instead of losing the release.

### With n8n (or any workflow engine)

The same thing without a host to maintain, and without touching an AI node —
three nodes:

1. **Schedule Trigger** — every 15 minutes.
2. **RSS Read** — `https://github.com/PegaProx/project-pegaprox/releases.atom`
   (public, no token, no API rate limit).
3. **Remove Duplicates** — in "keep items seen in previous executions" mode,
   keyed on the item `id`. This is n8n's own state; no code node needed.
4. **HTTP Request** — POST to the routine's webhook URL. Store it as an n8n
   credential, not inline in the node.

Whatever fires the webhook, the routine does the rest.

### The routine itself

Its prompt lives in [`routine-prompt.md`](routine-prompt.md) — that file is the
source of truth for what the routine is told to do; edit it there and update
the routine. A slow cron on the routine (weekly) is worth keeping as a safety
net in case the watcher host is down when a release lands.

**GitHub Actions is deliberately not used here.** Minutes are billed per
organization and simply stop when the allowance is used up — a watcher that
quietly dies is worse than no watcher.

## Current patch set

See `patches.yml`. As of the first sync:

- **vnc-console-under-gevent** — the web console never connected under gevent
  on Python 3.13+ (upstream PR #741)
- **taskbar-remount-expand** — the task bar popped open on page load and on
  every cluster switch (upstream PR #739)
- **corporate-theme-source-of-truth** — "Corporate Light" from the settings was
  overridden on reload (upstream issue #742)
