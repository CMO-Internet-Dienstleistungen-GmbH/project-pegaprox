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

### The first step runs by itself

Opening an issue starts a Claude Code routine. It reads the request, looks for
whether the thing already exists — in the other issues, in the code, in
`patches.yml` — classifies it A/B/C/D, and leaves either a rough plan or up to
three questions as a comment, in German. Answering it starts it again, so a
question and its answer stay in one thread.

| Label | What it means |
|---|---|
| `triage/A-config` … `triage/D-internal` | the category; exactly one is ever set |
| `needs-info` | a question is open, waiting on the requester |
| `needs-review` | the routine stopped and wants a decision |
| `triaged` | classified, plan is in the thread |

**It plans and asks; it does not build.** It writes no code, creates no branch,
pushes nothing, edits neither `patches.yml` nor any other file, and never closes
an issue or marks it `duplicate` or `wontfix` — a colleague's request does not get
dismissed by an automation. That limit is also what makes it safe to point a
language model at text other people wrote.

It stops after three rounds on one issue and hands over with `needs-review`.

Its prompt is [`triage-prompt.md`](triage-prompt.md) — that file is the source of
truth for what it is told to do; edit it there and update the routine.

A smaller self-hosted model was considered for the first pass and deferred: the
run does multi-step tool use (grep the code, read `patches.yml`, compare a patch
against a release tag) and has to recognise a prompt injection in text other
people wrote. What a small model could take over is the narrow part —
category plus reasoning as JSON against a schema, with a schema violation
escalating — but that drops the "does it already exist?" check, which is the
most useful thing the routine does. Worth revisiting once the volume justifies
running and watching another service.

Writing an issue requires being a collaborator on the fork: the repository is
public and interaction is limited to `collaborators_only`, so a colleague needs
`pull` access once, and nobody else can file anything.

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

One patch: **system-theme** — the "System" theme that follows the OS light/dark
setting. Upstream issue #743 is open and PR #745 was closed pending discussion,
so there is no PR to track; the entry has to be retired by hand once upstream
ships its own. The branch holds exactly one commit against the release tag.

The three original patches are gone, each solved upstream in a different shape
than our PR — which is why none of them was dropped automatically:

| Patch | Upstream |
|---|---|
| vnc-console-under-gevent | issue #740, fixed in 1.1.0 |
| taskbar-remount-expand | issue #738, fixed in 1.1.0 |
| corporate-theme-source-of-truth | issue #742, fixed in 1.1.0 (702bfa5 + c53a859); our PR #744 was closed, not merged |

### Keep the patch branches, and keep them atomic

A patch branch is the source `cmo/main` is rebuilt from — the integration branch
itself is disposable, the branches are not. Two rules follow:

- **Do not delete a branch that is still in the set.** Deleting it means the
  next sync cannot rebuild, and `cmo/main` is force-pushed, so there is nothing
  to recover it from.
- **One concern per branch, ideally one commit.** `feat/system-theme` used to
  carry six commits — a theme-registry refactor and the corporate fix riding
  along with the feature. When upstream fixed the corporate bug, none of it
  could be retired individually. It was rebuilt as a single commit against the
  release tag; that is the shape to aim for.
