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
# name, branch, base, upstream_pr, summary. Not TAB: `read` treats TAB as IFS
# whitespace and collapses runs of it, which drops empty fields.
cfg_patches() {
    python3 - "$CONFIG_FILE" <<'PY'
import sys, yaml
with open(sys.argv[1]) as fh:
    data = yaml.safe_load(fh) or {}
for p in data.get('patches') or []:
    print('\x1f'.join(str(p.get(k) or '') for k in ('name', 'branch', 'base', 'upstream_pr', 'summary')))
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
gh_get() {
    local url="$1"
    if [ -n "${GITHUB_TOKEN:-}" ]; then
        curl -fsSL -H "Authorization: Bearer ${GITHUB_TOKEN}" \
             -H 'Accept: application/vnd.github+json' "$url"
    else
        curl -fsSL -H 'Accept: application/vnd.github+json' "$url"
    fi
}

# Latest published (non-draft, non-prerelease) release tag of a repo.
latest_release_tag() {
    gh_get "https://api.github.com/repos/$1/releases/latest" \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"])'
}

# Whether a pull request is merged: pr_merged <repo> <number> -> "true"/"false"
pr_merged() {
    gh_get "https://api.github.com/repos/$1/pulls/$2" \
        | python3 -c 'import json,sys; print(str(json.load(sys.stdin).get("merged", False)).lower())' \
        2>/dev/null || echo unknown
}
