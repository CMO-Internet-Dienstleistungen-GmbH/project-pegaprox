# Project: PegaProx fork automation (`cmo/automation`)

The tooling that keeps the CMO fork of PegaProx in sync with upstream
releases — `patches.yml`, `scripts/`, `routine-prompt.md`. No application code
lives on this branch; see `README.md` for how a sync builds `v<release>-cmo.<n>`.

## Rules & Constraints

### Everything that leaves this repository is public

Upstream issues, pull requests, comments, commit messages on patch branches
and the diffs themselves land on github.com under the CMO name. Nothing
internal goes into them:

- **No hostnames, node names, cluster names, addresses or identifiers from
  our environment.** Not in prose, not in log excerpts, not in stack traces,
  not in test fixtures. `192.168.9.198`, `b06-c4-2`, `infra-pegaprox`,
  `PegaProx_c4`, a session or client UUID — all of it is replaced by
  placeholders (`<node>`, `<node-ip>`, `pegaprox-host`, `PegaProx_<cluster>`,
  `<uuid>`) *before* the text is written down, not after someone notices.
  `127.0.0.1` and our public fork URL are the only addresses that may appear.
- **Anonymise a log excerpt at the moment it is pasted**, and grep the
  finished text before handing it over:
  `grep -nE '[0-9]{1,3}(\.[0-9]{1,3}){3}|b06|c06|infra-|PegaProx_c[0-9]|cmo\.de'`
  must return nothing but loopback. Run the same grep over
  `git log -p upstream/main..<branch>` before a patch branch is pushed.
- Customer and topology facts (how many clusters, which storage, which
  network) stay out unless they are the bug. "Across our six clusters" is
  fine; the cluster names are not.
- Drafts for upstream communication are written to
  `~/Projekte/CMO/project-pegaprox-gh-upstream-communication/` as
  `ISSUE-<yyyy-mm-dd>-<topic>.md` / `PR-<yyyy-mm-dd>-<topic>.md`, checked
  against the live upstream templates (GitHub API, not the local clone), and
  opened by Dennis — never posted by the assistant.

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
