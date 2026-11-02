#!/usr/bin/env bash
# Shared helpers for the CMO fork automation. Sourced, never executed.

# Colours only when stdout is a terminal — routine logs stay readable.
if [ -t 1 ]; then
    C_RED=$'\033[0;31m'; C_GREEN=$'\033[0;32m'; C_YELLOW=$'\033[0;33m'
    C_BLUE=$'\033[0;34m'; C_OFF=$'\033[0m'
else
    C_RED=''; C_GREEN=''; C_YELLOW=''; C_BLUE=''; C_OFF=''
fi

log()   { printf '%s\n' "$*"; }
info()  { printf '%s==>%s %s\n' "$C_BLUE" "$C_OFF" "$*"; }
ok()    { printf '%s ok %s %s\n' "$C_GREEN" "$C_OFF" "$*"; }
warn()  { printf '%swarn%s %s\n' "$C_YELLOW" "$C_OFF" "$*" >&2; }
die()   { printf '%sfail%s %s\n' "$C_RED" "$C_OFF" "$*" >&2; exit 1; }

require_cmd() {
    local missing=0 c
    for c in "$@"; do
        command -v "$c" >/dev/null 2>&1 || { warn "required command not found: $c"; missing=1; }
    done
    [ "$missing" -eq 0 ] || die "install the missing tools and run again"
}

# Read one scalar from the YAML config: cfg <dotted.path>
cfg() {
    python3 - "$CONFIG_FILE" "$1" <<'PY'
import sys, yaml
with open(sys.argv[1]) as fh:
    data = yaml.safe_load(fh)
for key in sys.argv[2].split('.'):
    data = (data or {}).get(key)
print('' if data is None else data)
PY
}

# Emit the patch list, one record per line, fields separated by US (0x1f):
# name, branch, base, upstream_pr, summary, kind, requested_by, commit_message.
# Not TAB: `read`
# treats TAB as IFS whitespace and collapses runs of it, which drops empty fields.
#
# `kind` defaults to 'fix' when the entry does not say otherwise, so every
# pre-existing entry keeps behaving exactly as before.
cfg_patches() {
    python3 - "$CONFIG_FILE" <<'PY'
import sys, yaml
with open(sys.argv[1]) as fh:
    data = yaml.safe_load(fh) or {}
for p in data.get('patches') or []:
    fields = [str(p.get(k) or '') for k in ('name', 'branch', 'base', 'upstream_pr', 'summary')]
    fields.append(str(p.get('kind') or 'fix'))
    fields.append(str(p.get('requested_by') or ''))
    fields.append(str(p.get('commit_message') or ''))
    print('\x1f'.join(fields))
PY
}

# Emit generated files, one per line: path<US>rebuild-command
cfg_generated() {
    python3 - "$CONFIG_FILE" <<'PY'
import sys, yaml
with open(sys.argv[1]) as fh:
    data = yaml.safe_load(fh) or {}
for g in data.get('generated_files') or []:
    print('\x1f'.join(str(g.get(k) or '') for k in ('path', 'rebuild')))
PY
}

# GitHub REST GET without auth (both repos are public). Adds the token when
# GITHUB_TOKEN is set, purely to lift the anonymous rate limit — never required.
#
# NOTE: this is best-effort only. Sandboxed runners (Claude Code cloud) route
# egress through a proxy that answers 403 for repositories not attached to the
# session, so nothing essential may depend on it — see latest_release_tag(),
# which uses plain git instead.
gh_get() {
    local url="$1"
    if [ -n "${GITHUB_TOKEN:-}" ]; then
        curl -fsSL -H "Authorization: Bearer ${GITHUB_TOKEN}" \
             -H 'Accept: application/vnd.github+json' "$url"
    else
        curl -fsSL -H 'Accept: application/vnd.github+json' "$url"
    fi
}

# Highest release tag of a repo, discovered with plain git — no API, no token.
#
# `git ls-remote` works wherever a clone works, including sandboxes that block
# api.github.com for repositories they have not attached. Upstream tags every
# release as vMAJOR.MINOR[.PATCH[.FIX]] on main, so the version-sorted last
# match is the current release. The pattern deliberately rejects anything with
# a suffix: release candidates (v1.1-rc1) and our own downstream tags
# (v1.0.2-cmo.1) must never be mistaken for an upstream release.
latest_release_tag() {
    # Accepts either a clone URL or a bare owner/repo.
    local url="$1"
    case "$url" in
        *://*|git@*|/*|.*) : ;;
        */*)               url="https://github.com/$url.git" ;;
    esac
    git ls-remote --tags "$url" 'refs/tags/v*' 2>/dev/null \
        | sed 's|.*refs/tags/||; s|\^{}||' \
        | sort -u \
        | grep -E '^v[0-9]+(\.[0-9]+)*$' \
        | sort -V \
        | tail -1
}

# Whether a pull request is merged: pr_merged <repo> <number> -> true/false/unknown.
# "unknown" whenever the API is unreachable (no network, rate limit, sandbox
# proxy) — callers must treat it as "cannot tell" and fall back to the
# patch-id check, never as "not merged".
pr_merged() {
    gh_get "https://api.github.com/repos/$1/pulls/$2" \
        | python3 -c 'import json,sys; print(str(json.load(sys.stdin).get("merged", False)).lower())' \
        2>/dev/null || echo unknown
}

# The pytest-xdist the fork's CI installs on top of upstream's dev requirements.
# Pinned so a new xdist release cannot change the gate between two syncs.
CI_XDIST_PIN='pytest-xdist==3.8.0'

# The commit cmo/ci-base points at: ci_parallel_base <release sha> <workflow path>
#
# Upstream's test.yml runs its 15,000 tests one after another on a four-core
# runner, which took 50 minutes for v1.4.0. This returns a commit on top of the
# release whose only change is that workflow file, installing pytest-xdist next
# to upstream's dev requirements and running pytest with `-n auto`. Nothing else
# in the tree differs, and the tag never contains it: the build descends from
# the release, not from this commit.
#
# When upstream rewrites either of the two lines, it falls back to the plain
# release with a warning -- the suite then runs serially but still runs, so a
# reworded workflow costs time, never a wrong verdict.
#
# Author, committer and dates are fixed to the release commit, so the same
# release always gives the same sha and a re-run pushes nothing new to the base.
ci_parallel_base() {
    local base="$1" path="$2" mode blob tree idx changed
    mode="$(git -C "$REPO_ROOT" ls-tree "$base" -- "$path" | awk '{print $1}')"
    if [ -z "$mode" ]; then
        warn "$path is not in the release -- CI runs on the plain release"
        printf '%s\n' "$base"; return 0
    fi
    blob="$(git -C "$REPO_ROOT" cat-file blob "$base:$path" | sed -E \
        -e "s#^( *\\.venv/bin/pip install -r requirements\\.txt -r requirements-dev\\.txt)\$#\\1 $CI_XDIST_PIN#" \
        -e 's#^( *run: \.venv/bin/python -m pytest tests/ -q)$#\1 -n auto#' \
        | git -C "$REPO_ROOT" hash-object -w --stdin)"
    changed="$(git -C "$REPO_ROOT" diff --numstat "$base:$path" "$blob" | awk '{print $1}')"
    if [ "$changed" != "2" ]; then
        warn "upstream reworded the install or pytest line of $path -- CI runs the suite serially"
        printf '%s\n' "$base"; return 0
    fi
    idx="$(mktemp "${TMPDIR:-/tmp}/pegaprox-ci-index.XXXXXX")"
    GIT_INDEX_FILE="$idx" git -C "$REPO_ROOT" read-tree "$base"
    GIT_INDEX_FILE="$idx" git -C "$REPO_ROOT" update-index --cacheinfo "$mode,$blob,$path"
    tree="$(GIT_INDEX_FILE="$idx" git -C "$REPO_ROOT" write-tree)"
    rm -f "$idx"
    GIT_AUTHOR_NAME='PegaProx fork sync' GIT_AUTHOR_EMAIL='noreply@github.com' \
    GIT_COMMITTER_NAME='PegaProx fork sync' GIT_COMMITTER_EMAIL='noreply@github.com' \
    GIT_AUTHOR_DATE="$(git -C "$REPO_ROOT" log -1 --format=%cI "$base")" \
    GIT_COMMITTER_DATE="$(git -C "$REPO_ROOT" log -1 --format=%cI "$base")" \
        git -C "$REPO_ROOT" commit-tree "$tree" -p "$base" \
            -m "ci: run the suite on every core of the runner (fork CI only, never tagged)"
}
