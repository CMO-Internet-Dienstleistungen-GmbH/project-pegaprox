# Project: PegaProx fork automation (`cmo/automation`)

The tooling that keeps the CMO fork of PegaProx in sync with upstream
releases — `patches.yml`, `scripts/`, `routine-prompt.md`. No application code
lives on this branch; see `README.md` for how a sync builds `v<release>-cmo.<n>`.

## Rules & Constraints

### Everything that leaves this repository is public

Upstream issues, pull requests, comments, commit messages on patch branches
and the diffs themselves land on github.com under the CMO name. **So do the
issues in our own fork** — the repository is public, so an issue here is read
by anyone, not only by us. It is a scope document written in the open, and
the same rules apply to it as to anything sent upstream. Nothing internal
goes into any of them:

- **No hostnames, node names, cluster names, user names, domains, addresses,
  tokens or identifiers from our environment.** Not in prose, not in log
  excerpts, not in stack traces, not in test fixtures. `192.168.9.198`,
  `b06-c4-2`, `infra-pegaprox`, `PegaProx_c4`, a login name, an internal
  domain, a session or client UUID, an SSE token — all of it is replaced by
  placeholders (`<node>`, `<node-ip>`, `pegaprox-host`, `PegaProx_<cluster>`,
  `<user>`, `<uuid>`) *before* the text is written down, not after someone
  notices. `127.0.0.1` and our public fork URL are the only addresses that
  may appear.
- **Anonymise a log excerpt at the moment it is pasted**, and grep the
  finished text before handing it over:
  `grep -nE '[0-9]{1,3}(\.[0-9]{1,3}){3}|b06|c06|infra-|PegaProx_c[0-9]|cmo\.de'`
  must return nothing but loopback. Run the same grep over
  `git log -p upstream/main..<branch>` before a patch branch is pushed.
- Customer and topology facts (how many clusters, which storage, which
  network) stay out unless they are the bug. "Across our six clusters" is
  fine; the cluster names are not.
- **Versions, sizes and measurements are fine, and usually necessary.** "a
  1.1.0 instance", "219 VMs", request counts, payload sizes, response times,
  the rate a component documents against the one observed — that is what a
  finding stands on. Strip it and an issue becomes an assertion nobody can
  check, which is its own kind of failure. The line is not *how much* detail
  but *what kind*: a quantity describes the product and the load it is under;
  a **name** identifies our environment. Keep the numbers, replace the names.
- Drafts for upstream communication are written to
  `~/Projekte/CMO/project-pegaprox-gh-upstream-communication/` as
  `ISSUE-<yyyy-mm-dd>-<topic>.md` / `PR-<yyyy-mm-dd>-<topic>.md`, checked
  against the live upstream templates (GitHub API, not the local clone), and
  opened by Dennis — never posted by the assistant.

Which values that splits into:

| Goes in | Stays out |
|---|---|
| **Versions** — `1.1.0`, `v1.1.0-cmo.3`, `Python 3.14.4`, `gevent 26.8.0`, a PVE or PBS version | **Domains and hostnames** — anything that resolves, ours or a customer's |
| **Counts** — `219 VMs`, `5 nodes`, `six clusters`, `200 rows per page` | **Node names** — `b06-c3-1`, `c06-c1-4` |
| **Measurements** — `867 calls / 221 s`, `10.9 MB`, `p50 3.75 s`, `40 % duplicates`, `22 in one second` | **Cluster and pool names** — `PegaProx_c4` |
| **The product's own values** — `Cache-Control: no-store`, a TTL, an interval, a pool size, a limit read out of the code | **Addresses** — `192.168.9.198`; `127.0.0.1` is the only one that may appear |
| **OS and runtime** — `Ubuntu 26.04`, an installation method | **People** — account names, mail addresses, anything naming a colleague or a customer |
| **Endpoint paths and payload shapes** of the product itself | **Secrets and handles** — session and SSE tokens, API tokens, client UUIDs |
| | **Storage and backup targets** — `pbs02-…`, a datastore name |

Some of it is a judgement call, and the way to make it is to ask what the
value would let a stranger do. A quantity lets them reproduce the finding. A
name lets them find our machine. When a log line carries both — and it
usually does — the numbers stay and the names become placeholders.

### What a patch has to look like

A patch is replayed onto every future upstream release, so its shape decides
what each release costs. These hold for every entry in `patches.yml`:

- **Additive, not invasive** — a new file or module. What upstream does not
  know about can never conflict.
- **Where touching an upstream file is unavoidable, leave a one-line hook**
  and put the logic in a file of your own. Minimise the conflict surface, not
  the line count.
- **No refactoring of upstream code in the same patch.** That is what got
  PR #744 closed, and it multiplies the conflict surface.
- **Never edit `web/index.html`** — it is a build artefact. Edit `web/src/*.js`
  and let the sync rebuild the bundle.
- **One patch, one concern, one branch.** Otherwise the sync cannot retire it
  on its own when upstream absorbs half of it.
- **Leave `.github/workflows/` alone.** A `fix/*` branch goes upstream as a
  pull request, and a diff touching CI config will not be accepted. What the
  fork needs differently is a repository setting, not a commit.
- **Fill in `upstream_pr` whenever a PR exists.** It is the only drop check
  that has ever fired here: the patch-id comparison recognises our commits
  only if they were taken as they are, and the maintainer has so far
  reimplemented every one of them. Without it, a patch fixed upstream long ago
  is replayed until somebody notices by hand.

### Upstream's own conditions

- Read `CONTRIBUTING.md` and `.github/` **live from upstream** (API, branches
  `main` and `Testing`) before drafting; the clone's copy can lag.
- Blank issues are disabled: bug or feature template, filled in full.
- A non-trivial PR needs an issue first. The PR template requires naming the
  AI assistant and model when the AI-assist box is ticked.
