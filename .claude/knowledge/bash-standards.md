---
artifacts:
    - customization_required: {}
      description: Template for a bash script with correct strict-mode setup and common patterns
      name: strict-mode-template.sh
      target_hint: Use as starting point for new bash scripts
category: Languages
complements:
    - security-standards
description: Bash strict-mode pitfalls, POSIX compatibility, cross-platform gotchas, and coding patterns
file_patterns:
    - '**/*.sh'
    - '**/Makefile'
    - '**/Justfile'
keywords:
    - bash
    - shell
    - sh
    - script
    - posix
    - automation
last_updated: "2026-09-30"
related_skills:
    - /bash
source_projects:
    - nextcloud
subcategory: Shell
technologies:
    - bash
    - sh
    - posix
    - shell
topic: bash-standards
---
# Bash Standards

## strict-mode Pitfalls

- `[[ cond ]] && cmd` with `set -e` aborts when condition is false (non-zero exit). Use `if [[ cond ]]; then cmd; fi`.
- `(( i++ ))` returns exit code 1 when i=0 (falsy). Use `i=$(( i + 1 ))`.
- `until` loop exit code = last body statement, not 0.
- `set -euo pipefail` — `pipefail` is bash-only, not in POSIX sh/BusyBox ash. `#!/bin/sh` scripts: `set -eu` only.
- Commands using non-zero exit for "not found" (grep, diff, pgrep) kill script under `set -e` and also break in the middle of a pipeline under `pipefail`. Suppress with `|| true` only when "no match" is expected — and watch for pipeline cases: `cmd | grep foo | wc -l` fails under `set -euo pipefail` whenever grep has no matches.
- `trap <handler> RETURN` under `set -u` fails if the handler references a variable (e.g. `tmpdir`) that is unset on some code paths — the trap fires on function return regardless of which branch ran. Initialise such variables to a sentinel before installing the trap: `local tmpdir=""; trap 'rm -rf "$tmpdir"' RETURN`.
- Version strings displayed as `vv1.x.y` (double `v`) happen when a `v` prefix is prepended to a variable that already contains `v`. Either strip the prefix beforehand (`ver="${ver#v}"`) or omit it in the format string — never do both.

## POSIX sh Compatibility (Alpine ash, BusyBox)

Scripts with `#!/bin/sh` MUST use only POSIX syntax — no `read -ra` (arrays), no here-strings (`<<<`), no `${!arr[@]}`, no `+=` for arrays, no `mapfile`/`readarray`, no `${var^^}`/`${var,,}`, no `read -t 0.1` fractional timeout. Use `while IFS= read -r line; do arr+=("$line"); done` instead of `mapfile`.

## Cross-Platform Gotchas

- `\s` fails on macOS BSD sed — use `[[:space:]]`. `sed -i` requires extension arg on macOS (`sed -i '' ...`), not on GNU sed.
- `read -i` (prefill) requires Bash 4+. Guard with `BASH_VERSINFO`. `BASH_SOURCE`/`set -E` are bash-specific — use `${BASH_SOURCE[0]:-$0}` as fallback.
- macOS ships Bash 3.2. `#!/usr/bin/env bash` resolves to 3.2 unless Homebrew bin precedes `/usr/bin` in `$PATH`.
- `cp -r src/ dest/` on BusyBox creates nested `dest/src/` when dest exists. Use `rsync -rltDog` for idempotent syncing.
- Alpine has no `/bin/bash` — scripts targeting Alpine must use `#!/bin/sh` with POSIX syntax.

## File Descriptors & Library Patterns

- Redirect stdin of the whole `while` loop (`done < "${file}"`), not per-read — per-read redirects reset file position to zero.
- `exec N<` fd assignment in nested subshells does not propagate to parent.
- Double-source guard: `if [[ -n "${_MYLIB_LOADED:-}" ]]; then return 0; fi; readonly _MYLIB_LOADED=1`
- Direct execution guard: `if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then echo "error: source, not execute" >&2; exit 1; fi`

## Security & Error Handling

- Check prerequisites at script top (required commands, files, env vars) and exit with clear message.
- Never use `set +e`. Handle explicitly with `|| true` or conditional checks.
- Never store secrets in scripts. Read from env vars, files, or prompts. Never pass secrets as CLI args (visible in `ps`) — use env vars or stdin.
- Set restrictive permissions on generated secrets files: `chmod 600`.

## Safety Gotchas

- find with a for-loop that searches subdirectories can delete directories containing real files if the find predicate is applied at the wrong directory level — verify the scope before running rm -rf in loops.
- A dangling symlink (e.g. agents/transcript-reader.md pointing to a deleted target) is silently ignored by most tools but breaks any script that dereferences it; remove dangling symlinks explicitly after deleting their targets.
- Shell vars used by PHP scripts (via getenv()) must be exported with `export` in startup scripts; unexported vars are invisible to child processes including PHP.
- GNU `patch` truncates the written file to the line count stated in the unified-diff hunk header (`@@ -0,0 +1,N @@`). If `N` is smaller than the actual number of `+` lines, the file is silently cut short causing syntax errors. Always verify the hunk header matches the real line count, especially when generating patches by hand or with imperfect tooling.

## Wait-Loops & Polling auf Hintergrund-Arbeit

- **GitLab-Pipelines-API `?sha=` verlangt die VOLLE 40-Zeichen-SHA.** Eine Kurz-SHA (z. B. `git log --oneline`-Ausgabe) liefert still `[]` — ein Poll-Loop wartet dann endlos auf "none". Vor dem API-Call expandieren: `git rev-parse --verify "<short>^{commit}"`, Fallback `GET /projects/:id/repository/commits/<short>` (`.id`).
- **Retryable vs. terminal sauber trennen:** "Noch keine Pipeline" ist nur retrybar, wenn der Commit remote überhaupt existiert. Unbekannte Referenz = terminaler Fehler (exit ≠ retry-Code), sonst pollt der Aufrufer auf ein Ereignis, das nie eintreten kann.
- **Readiness-Polling braucht IMMER einen Liveness-Check daneben.** `until curl -sf $HEALTH; do sleep 2; done` hängt ewig, wenn der Prozess/Container beim Start gecrasht ist. Im Loop zusätzlich prüfen und terminal abbrechen: Container → `docker inspect -f '{{.State.Running}}'` bzw. `docker ps`-Status; Prozess → `kill -0 $PID`. Verifiziert 2026-07: ein Testcontainer crashte an fehlenden Pflicht-Envs, der reine Health-Loop wartete eine Nacht lang.
- **Kein Watcher auf die Ausgabe einer Kette, deren erster Schritt still scheitern kann.** Bei `docker run … && until …`-Ketten im Hintergrund: schlägt `docker run` fehl, bleibt die Ausgabedatei leer und jeder Monitor darauf ist blind. Direkt nach dem Start den Zustand des gestarteten Dings verifizieren (`docker ps -a --filter name=…`, Exit-Code), erst DANN einen Watcher armieren.
- **Ein Poll-Loop braucht ein Zeitbudget.** Auch bei korrekten Bedingungen: `for i in $(seq 1 N); do …; done` mit terminalem Fehlerpfad statt `until`-Endlosschleife, wenn das erwartete Ereignis nicht garantiert eintritt.

## Promoted (2026-08-15)

- [GOTCHA:0.85] Bash printf '%.2f' breaks on German locale because it expects comma as decimal separator; use LC_ALL=C printf or awk for floating-point formatting in locale-agnostic scripts.
- [PATTERN:0.85] When concatenating multi-part large files sequentially (no full disk headroom for simultaneous parts+merged), use 'dd if=part of=merged bs=64M oflag=append conv=notrunc status=progress' instead of cat; gives ~10-25% speed improvement on NVMe and live progress output.
- [GOTCHA:0.95] With 'set -o pipefail' in bash, 'cmd | head -N' causes cmd to receive SIGPIPE and exit 141, tripping pipefail and killing the script even when cmd succeeded — always write to a tempfile and read with head separately
- [PATTERN:0.85] For idempotent multi-stage build scripts (quantize, split, upload), use per-phase marker files and atomic writes (write to .partial then mv) to enable safe crash recovery and re-run without redundant work
- [GOTCHA:0.95] curl -sf treats HTTP 4xx (including 403) as failure (exit 22), so a reachability check against an auth-required S3 endpoint will always report 'not reachable' even when the server is up and healthy.
- [GOTCHA:0.95] In bats tests, an ERR/EXIT trap set inside the script under test fires on expected errors and tears down the bats runner; defuse the trap in test setup to prevent false failures.
- [GOTCHA:0.95] In bats, using `run` with a function that calls `exit` directly terminates the test shell instead of being captured; wrap such calls in a subshell (e.g. `run bash -c '...'`) to isolate the exit.
- [GOTCHA:0.85] A `readonly` global variable cannot be overridden in a bats test environment; replace it with a function parameter so tests can supply different values without hitting a readonly assignment error.
- [GOTCHA:0.90] ShellCheck SC2034 false positives occur when sourced library variables are only used in other sourced files; ShellCheck resolves source paths relative to CWD and misses cross-file usage.
- [GOTCHA:0.95] Running `sshd -T` and `getent shadow` as an unprivileged user silently fails when stderr is redirected with `2>/dev/null`, causing empty output to be misinterpreted as test failure — these commands require root.
- [GOTCHA:0.92] grep -c returns exit code 1 when there are zero matches, so `grep -c ... || echo 0` appends a second '0' on a new line instead of replacing the output, resulting in '0\n0' rather than '0' and causing numeric comparisons to fail even when the result is correct.
- [GOTCHA:0.90] 'sudo rm -f <dir>/*' on a mode-0700 directory silently does nothing and still exits 0, because the shell glob expands before sudo, producing no arguments — use 'sudo rm -rf <dir>' or run the glob inside sudo bash -c.
- [GOTCHA:0.85] macOS does not ship `timeout`; scripts that rely on it will fail silently — use `perl -e 'alarm N; exec @ARGV'` or a background-kill pattern instead.
- [CONVENTION:0.90] Only modify the specific hook field in a foreign-managed config file; never rewrite or reformat the entire file even if the result is semantically equivalent.
- [PATTERN:0.85] Use a snapshot-isolated Git worktree for autonomous curation agents: each tool call commits to a temp branch, giving diff/rewind/branch for free without touching the live corpus until the user explicitly approves.
- [GOTCHA:0.95] Shell function wrappers (e.g. `ssh()`) only intercept direct `ssh` calls; `scp` and `sftp` bypass them entirely and never receive the injected environment variables (SSH_ASKPASS etc.).
- [PATTERN:0.85] Use `git status --porcelain=v2 --branch -z` combined with `--numstat` and `git log` to retrieve branch name, upstream, ahead/behind counts, and all file changes in a single git invocation.
- [PATTERN:0.85] Use 'git ls-files' instead of filesystem enumeration to build a workspace file tree — .gitignore is respected automatically without needing to parse it.
- [GOTCHA:0.85] git log --shortstat prints the stat line AFTER the formatted commit line, so a separator placed after the format string attaches each commit's numbers to the commit below it; the separator must be placed before the format string instead.

## Scripts read secrets from env or stdin, never from 1Password

A delivered script never calls `op` or any other secret-store CLI. It reads its secrets from environment variables or stdin and reports a missing one by name, so the caller decides where they come from. CI/CD pipelines have no 1Password at all. Locally the caller may wrap the script, e.g. `op run -- ./scripts/run.sh apply`; that is one documented option in the README, never part of the code path.

## Script configuration chain — reference implementation

The global configuration order for scripts: `~/.config/<tool>.env` (personal defaults) → environment profile via `--env <name>` / `--env-file` / `<TOOL>_ENV`, searched in the project's `environments/` only → the caller's own environment, which always wins.

## Variable files live in the project (2026-09-30)

Environment profiles, tfvars and Ansible var files of a concrete environment belong **inside the project** (`environments/<env>.env|.tfvars|.yml`), never in `~/.config`. In a git repository they are git-ignored (`environments/*` + `!environments/example.*`), only the templates are tracked. Profile lookup searches `environments/` only. `~/.config/<tool>.env` remains for personal defaults: scripts read it, but the agent never creates or writes it. SSH keys and other secrets are credentials, not variable files, and stay outside the project.
