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

## Where an internal request goes

A request from a colleague is classified before a line of code is written,
because the category decides what it costs us on *every* future release:

| Category | Route | Cost per upstream release |
|---|---|---|
| **A — configuration** | config/ENV/theme, no code | none |
| **B — bug** | issue upstream, PR against `Testing`, entry here with `upstream_pr` | temporary — drops itself |
| **C — generic feature** | build it here **and** offer it upstream in parallel | temporary; rework if upstream builds it differently |
| **D — CMO-specific** | stays in the fork for good, `kind: internal` | permanent |

C is deliberately not "wait for upstream to agree first": being blocked for
weeks on a discussion costs more than the occasional rework. Keep such a patch
small and additive so that rework stays cheap.

The chain ends at the tag:

```
request -> issue in the fork -> triage A/B/C/D
        -> branch + entry in patches.yml
        -> sync builds v<upstream>-cmo.<n>, tests green, tag pushed
        -> notification: a new version is ready to test
```

What happens after that — testing it, rolling it out, moving
`pegaprox_install_version` — belongs to the IaC repository, not here.

### Rules for a category D patch

A permanent patch has to survive a rebase onto every future upstream release.
What makes that cheap:

1. **Additive, not invasive** — a new file or module. What upstream does not
   know about can never conflict.
2. **Where touching an upstream file is unavoidable, leave a one-line hook** and
   put the logic in a file of your own. Minimise the conflict surface, not the
   line count.
3. **No refactoring of upstream code** in the same patch. That is what got PR
   #744 closed, and it multiplies the conflict surface.
4. **Never edit `web/index.html`** — it is a build artefact; edit `web/src/*.js`
   and let the sync rebuild the bundle.
5. **One patch, one concern, one branch** — otherwise the sync cannot drop it
   individually.
6. **Leave `.github/workflows/` alone.** A `fix/*` branch goes upstream as a PR,
   and a diff that touches CI config will not be accepted. Anything the fork
   needs differently is a repository setting, not a commit.

## Adding, changing or retiring a patch

Everything lives in [`patches.yml`](patches.yml) — the scripts hardcode nothing:

```yaml
patches:
  - name: some-fix
    branch: fix/some-fix        # branch in the fork holding the commits
    base: Testing               # upstream branch it was built on
    kind: fix                   # fix | feature | internal   (default: fix)
    upstream_pr: 812            # drops the patch automatically once merged
    requested_by: 12            # issue in the fork; for internal patches
    summary: one line for the tag message
```

`kind` is the promise the entry makes. `fix` and `feature` are meant to end up
upstream and disappear from this file; `internal` is the opposite — it stays in
the fork, so neither drop heuristic may touch it, and a branch with no commits
is an error rather than a silent skip. An unknown value stops the run: a typo
must not quietly turn a permanent patch back into a droppable one.

The commits taken are `merge-base(upstream/<base>, origin/<branch>)..<branch>`,
so rebasing the fix branch onto a newer upstream is transparent here.

When a fix lands upstream, you do not have to do anything: the next sync sees
the merged PR and drops it. Deleting the entry afterwards keeps the file tidy.

In practice the `upstream_pr` lookup is the one that fires. The patch-id check
(`git cherry`) only recognises our commits when they were taken *as they are* —
and so far the maintainer has reimplemented every one of our fixes in a revised
form, which gives them a different patch-id. So keep `upstream_pr` filled in:
without it, a patch that is long since fixed upstream will be replayed until
someone notices by hand.

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

See `patches.yml` — it is the authority; this list is a summary.

**As of v1.1.0 the set is empty: the fork carries no delta against upstream.**
All three patches were solved upstream, each in a different shape than our PR,
which is why none of them was dropped automatically:

| Patch | Upstream |
|---|---|
| vnc-console-under-gevent | issue #740, fixed in 1.1.0 |
| taskbar-remount-expand | issue #738, fixed in 1.1.0 |
| corporate-theme-source-of-truth | issue #742, fixed in 1.1.0 (702bfa5 + c53a859); our PR #744 was closed, not merged |

The fork stays useful as the vehicle for the next patch — and the tag remains
what gets deployed, so the deployed revision is still unambiguous.

Still open upstream: the **System theme** that follows the OS light/dark
setting (issue #743, PR #745 closed pending discussion). The branch
`feat/system-theme` holds the work; it is not in the patch set.
