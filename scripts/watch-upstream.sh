#!/usr/bin/env bash
#
# Notice a new upstream release and fire a webhook. Nothing else.
#
# This is the cheap half of the automation: a plain string comparison that can
# run every few minutes on any box, so the expensive half (the Claude Code
# routine that actually rebuilds and tags) only ever starts when there really
# is a new release.
#
# Deliberately dependency-free: `git ls-remote` and `curl`, no GitHub API, no
# token, no clone. Safe to run from cron or a systemd timer.
#
#   WEBHOOK_URL=https://... ./scripts/watch-upstream.sh
#
# State lives in a single file (default: ~/.local/state/pegaprox-upstream-release).
# On the very first run it records the current release WITHOUT firing, so
# setting this up does not immediately trigger a rebuild.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$SCRIPT_DIR/lib.sh"

CONFIG_FILE="${SYNC_CONFIG:-$REPO_ROOT/patches.yml}"
STATE_FILE="${STATE_FILE:-${XDG_STATE_HOME:-$HOME/.local/state}/pegaprox-upstream-release}"
DRY_RUN=0

usage() {
    cat <<'EOF'
Usage: watch-upstream.sh [--dry-run] [--state PATH]

Compares the highest upstream release tag against the last one seen and, when
it changed, POSTs to $WEBHOOK_URL. Prints one line either way.

Options:
  --dry-run     Report what would happen; fire nothing, write no state.
  --state PATH  State file (default: $XDG_STATE_HOME/pegaprox-upstream-release
                or ~/.local/state/pegaprox-upstream-release).
  -h, --help    This text.

Environment:
  WEBHOOK_URL   Required unless --dry-run. The Claude Code routine's webhook
                URL. Keep it out of the repository and out of argv — it is a
                capability: anyone holding it can start the routine.
  SYNC_CONFIG   Alternative patches.yml (only upstream.url is read here).

Exit codes:
  0  no new release, or fired successfully
  1  error (webhook unreachable, upstream unreadable, misconfiguration)

cron, every 15 minutes:
  */15 * * * * WEBHOOK_URL=$(cat ~/.config/pegaprox-webhook) \
               /srv/pegaprox-automation/scripts/watch-upstream.sh >> /var/log/pegaprox-watch.log 2>&1
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)  DRY_RUN=1 ;;
        --state)    shift; STATE_FILE="${1:-}" ;;
        -h|--help)  usage; exit 0 ;;
        *)          usage >&2; die "unknown argument: $1" ;;
    esac
    shift
done

require_cmd git curl
[ -f "$CONFIG_FILE" ] || die "config not found: $CONFIG_FILE"
python3 -c 'import yaml' 2>/dev/null || die "python3 needs PyYAML (pip install pyyaml)"

UPSTREAM_URL="$(cfg upstream.url)"
CURRENT="$(latest_release_tag "$UPSTREAM_URL")"
[ -n "$CURRENT" ] || die "could not read any release tag from $UPSTREAM_URL"

PREVIOUS=""
[ -f "$STATE_FILE" ] && PREVIOUS="$(cat "$STATE_FILE")"

if [ "$CURRENT" = "$PREVIOUS" ]; then
    log "$(date -u +%FT%TZ) no change: $CURRENT"
    exit 0
fi

if [ -z "$PREVIOUS" ]; then
    # First run: remember where we are instead of firing for a release that is
    # probably already built.
    if [ "$DRY_RUN" -eq 1 ]; then
        log "$(date -u +%FT%TZ) would initialise state at $CURRENT (no webhook on first run)"
        exit 0
    fi
    mkdir -p "$(dirname "$STATE_FILE")"
    printf '%s\n' "$CURRENT" > "$STATE_FILE"
    log "$(date -u +%FT%TZ) initialised at $CURRENT (no webhook fired)"
    exit 0
fi

log "$(date -u +%FT%TZ) new upstream release: $PREVIOUS -> $CURRENT"

if [ "$DRY_RUN" -eq 1 ]; then
    log "would POST to \$WEBHOOK_URL (dry run, state unchanged)"
    exit 0
fi

[ -n "${WEBHOOK_URL:-}" ] || die "WEBHOOK_URL is not set"

# The state file is written only after the webhook was accepted, so a failed
# call is retried on the next tick instead of being silently swallowed.
status="$(curl -sS -o /dev/null -w '%{http_code}' \
    --retry 3 --retry-delay 5 --max-time 30 \
    -X POST -H 'Content-Type: application/json' \
    -d "{\"upstream_release\":\"$CURRENT\",\"previous\":\"$PREVIOUS\"}" \
    "$WEBHOOK_URL")" || die "webhook call failed (network)"

case "$status" in
    2*) ok "webhook accepted ($status)" ;;
    *)  die "webhook returned $status — state not advanced, will retry next run" ;;
esac

mkdir -p "$(dirname "$STATE_FILE")"
printf '%s\n' "$CURRENT" > "$STATE_FILE"
log "state advanced to $CURRENT"
