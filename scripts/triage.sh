#!/usr/bin/env bash
#
# The mechanical half of issue triage. Everything here is decided by rules, not
# by judgement, so it belongs in a script rather than in a prompt: counting the
# rounds, appending the marker, keeping the triage/* labels mutually exclusive,
# and stripping the footer the platform appends to a posted comment.
#
# What is deliberately NOT here: writing the comment and picking the category.
# That is the judgement, and it stays with whoever calls this.
#
# Posting is not here either, and that is not an oversight. The author of a
# comment is decided by the credentials that post it — the MCP GitHub tool
# posts as the maintainer, `gh` posts as claude[bot] — so the caller posts
# through the tool that gives the right author, then hands the comment id to
# `finish`.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$SCRIPT_DIR/lib.sh"

MARKER_PREFIX='<!-- triage-bot:round:'
SIGN_OFF='_Automatische Ersteinschätzung — ein Maintainer schaut noch drauf._'
MAX_ROUNDS=3

CATEGORY_LABELS=(triage/A-config triage/B-bug triage/C-feature triage/D-internal)
STATE_LABELS=(needs-info needs-review triaged)

usage() {
    cat <<'EOF'
Usage: triage.sh <command> --issue <n> [options]

Commands:
  gate      May the routine act on this issue at all? Prints the reason on
            stdout either way.
            Exit 0 = go ahead, 10 = stop and change nothing,
                 11 = round limit reached, escalate with needs-review.

  render    Read the comment text on stdin, print it back with the sign-off
            line and the round marker appended. Does not post anything.

  finish    After the comment has been posted: strip the platform footer from
            it, set the labels, and verify both. Needs --comment-id.
            Exit 0 = comment is clean and labelled.

Options:
  --issue <n>        issue number in the fork                       (required)
  --comment-id <id>  the id the post returned                    (finish only)
  --category <A|B|C|D>   sets exactly one triage/* label, removes the others
  --state <triaged|needs-info|needs-review>
                     sets that state label and removes the other two
  --repo <owner/name>    default: taken from the origin remote
  -h                 this text

Typical use:

  ./scripts/triage.sh gate --issue 7 || exit 0
  BODY="$(printf '%s' "$text" | ./scripts/triage.sh render --issue 7)"
  # ... post $BODY through the tool that gives the right author ...
  ./scripts/triage.sh finish --issue 7 --comment-id 123 --category C --state triaged
EOF
}

# ----------------------------------------------------------------- arguments

ISSUE=''
COMMENT_ID=''
CATEGORY=''
STATE=''
REPO=''

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --issue)      ISSUE="${2:-}";      shift 2 ;;
            --comment-id) COMMENT_ID="${2:-}"; shift 2 ;;
            --category)   CATEGORY="${2:-}";   shift 2 ;;
            --state)      STATE="${2:-}";      shift 2 ;;
            --repo)       REPO="${2:-}";       shift 2 ;;
            -h|--help)    usage; exit 0 ;;
            *)            usage >&2; die "unknown option: $1" ;;
        esac
    done

    [ -n "$ISSUE" ] || { usage >&2; die "--issue is required"; }
    case "$ISSUE" in
        ''|*[!0-9]*) die "--issue must be a number, got '$ISSUE'" ;;
    esac

    if [ -z "$REPO" ]; then
        REPO="$(git -C "$REPO_ROOT" remote get-url origin 2>/dev/null \
                | sed -E 's|^git@github\.com:||; s|^https://github\.com/||; s|\.git$||')"
        [ -n "$REPO" ] || die "cannot derive the repository from origin; pass --repo"
    fi

    if [ -n "$CATEGORY" ]; then
        case "$CATEGORY" in
            A|B|C|D) : ;;
            *) die "--category must be A, B, C or D, got '$CATEGORY'" ;;
        esac
    fi

    if [ -n "$STATE" ]; then
        local known=0 s
        for s in "${STATE_LABELS[@]}"; do [ "$s" = "$STATE" ] && known=1; done
        [ "$known" -eq 1 ] || die "--state must be one of: ${STATE_LABELS[*]}"
    fi
}

category_label() {
    case "$1" in
        A) printf 'triage/A-config' ;;
        B) printf 'triage/B-bug' ;;
        C) printf 'triage/C-feature' ;;
        D) printf 'triage/D-internal' ;;
    esac
}

# --------------------------------------------------------------------- reads

issue_json() {
    gh api "repos/$REPO/issues/$ISSUE" 2>/dev/null \
        || die "cannot read issue #$ISSUE in $REPO"
}

comments_json() {
    gh api --paginate "repos/$REPO/issues/$ISSUE/comments" 2>/dev/null \
        || die "cannot read the comments of issue #$ISSUE"
}

# How many comments in the thread already carry our marker.
count_rounds() {
    comments_json | python3 -c "
import json, sys
marker = sys.argv[1]
print(sum(1 for c in json.load(sys.stdin) if marker in (c.get('body') or '')))
" "$MARKER_PREFIX"
}

has_label() {
    issue_json | python3 -c "
import json, sys
labels = {l['name'] for l in json.load(sys.stdin).get('labels', [])}
sys.exit(0 if sys.argv[1] in labels else 1)
" "$1"
}

# ---------------------------------------------------------------------- gate

cmd_gate() {
    local state rounds
    state="$(issue_json | python3 -c 'import json,sys; print(json.load(sys.stdin)["state"])')"

    if [ "$state" != "open" ]; then
        log "issue #$ISSUE is $state — nothing to do"
        return 10
    fi

    if has_label needs-review; then
        log "issue #$ISSUE carries needs-review — it is waiting for a human"
        return 10
    fi

    # The newest comment being ours means we would be answering ourselves.
    local last_is_ours
    last_is_ours="$(comments_json | python3 -c "
import json, sys
comments = json.load(sys.stdin)
marker = sys.argv[1]
print('yes' if comments and marker in (comments[-1].get('body') or '') else 'no')
" "$MARKER_PREFIX")"
    if [ "$last_is_ours" = "yes" ]; then
        log "the newest comment on #$ISSUE is our own — answering it would loop"
        return 10
    fi

    rounds="$(count_rounds)"
    if [ "$rounds" -ge "$MAX_ROUNDS" ]; then
        log "issue #$ISSUE has $rounds rounds — the limit is $MAX_ROUNDS"
        return 11
    fi

    log "issue #$ISSUE is open, $rounds round(s) so far — go ahead"
    return 0
}

# -------------------------------------------------------------------- render

cmd_render() {
    local body rounds next
    body="$(cat)"
    [ -n "${body//[[:space:]]/}" ] || die "refusing to render an empty comment"

    case "$body" in
        *"$MARKER_PREFIX"*) die "the text already carries a round marker — pass the text only" ;;
    esac

    rounds="$(count_rounds)"
    next=$((rounds + 1))

    # Trailing blank lines would put an empty line between text and sign-off.
    printf '%s\n' "$body" | sed -e :a -e '/^\n*$/{$d;N;ba' -e '}'
    printf '\n%s\n\n%s%s -->\n' "$SIGN_OFF" "$MARKER_PREFIX" "$next"
}

# -------------------------------------------------------------------- finish

# The platform appends "---\n_Generated by [Claude Code](…)_" to a comment it
# posts. Cut exactly that trailer, nothing else, and only when it is there.
strip_footer() {
    local id="$1" body clean
    body="$(gh api "repos/$REPO/issues/comments/$id" --jq '.body' 2>/dev/null)" \
        || die "cannot read comment $id"

    clean="$(printf '%s' "$body" | python3 -c "
import re, sys
body = sys.stdin.read()
sys.stdout.write(re.sub(r'\n+-{3,}\n+_Generated by \[Claude Code\]\([^)]*\)_\s*\Z', '', body))
")"

    if [ "$clean" = "$body" ]; then
        ok "comment $id carries no footer"
        return 0
    fi

    local status
    status="$(printf '%s' "$clean" \
        | gh api --method PATCH "repos/$REPO/issues/comments/$id" -F body=@- \
                 --silent --include 2>&1 | head -1 || true)"

    body="$(gh api "repos/$REPO/issues/comments/$id" --jq '.body' 2>/dev/null || echo "$body")"
    case "$body" in
        *'_Generated by [Claude Code]'*)
            warn "the footer on comment $id survived the edit — $status"
            return 1 ;;
    esac
    ok "footer removed from comment $id"
}

apply_labels() {
    local add=() remove=() l
    if [ -n "$CATEGORY" ]; then
        local keep
        keep="$(category_label "$CATEGORY")"
        add+=("$keep")
        for l in "${CATEGORY_LABELS[@]}"; do
            [ "$l" = "$keep" ] || remove+=("$l")
        done
    fi
    if [ -n "$STATE" ]; then
        add+=("$STATE")
        for l in "${STATE_LABELS[@]}"; do
            [ "$l" = "$STATE" ] || remove+=("$l")
        done
    fi
    [ "${#add[@]}" -gt 0 ] || return 0

    # gh issue edit URL-encodes the slash in triage/*; the REST label endpoint
    # does not, which is why this goes through gh and not through gh api.
    local args=()
    for l in "${add[@]}";    do args+=(--add-label "$l"); done
    for l in "${remove[@]}"; do args+=(--remove-label "$l"); done
    gh issue edit "$ISSUE" --repo "$REPO" "${args[@]}" >/dev/null \
        || die "cannot set the labels on #$ISSUE"
    ok "labels: +${add[*]}"
}

verify_labels() {
    local want failed=0 l
    [ -n "$CATEGORY" ] && { want="$(category_label "$CATEGORY")"
        has_label "$want" || { warn "label $want is not set"; failed=1; }; }
    [ -n "$STATE" ] && { has_label "$STATE" || { warn "label $STATE is not set"; failed=1; }; }

    if [ -n "$CATEGORY" ]; then
        want="$(category_label "$CATEGORY")"
        for l in "${CATEGORY_LABELS[@]}"; do
            [ "$l" = "$want" ] && continue
            has_label "$l" && { warn "leftover category label: $l"; failed=1; }
        done
    fi
    return "$failed"
}

cmd_finish() {
    [ -n "$COMMENT_ID" ] || die "finish needs --comment-id"
    case "$COMMENT_ID" in
        ''|*[!0-9]*) die "--comment-id must be a number, got '$COMMENT_ID'" ;;
    esac

    local rc=0
    strip_footer "$COMMENT_ID" || rc=1
    apply_labels
    verify_labels || rc=1
    [ "$rc" -eq 0 ] || warn "finish completed with problems — see above"
    return "$rc"
}

# ---------------------------------------------------------------------- main

main() {
    local cmd="${1:-}"
    [ $# -gt 0 ] && shift
    case "$cmd" in
        gate|render|finish) : ;;
        -h|--help|'') usage; exit 0 ;;
        *) usage >&2; die "unknown command: $cmd" ;;
    esac

    require_cmd gh python3 git
    parse_args "$@"

    case "$cmd" in
        gate)   cmd_gate ;;
        render) cmd_render ;;
        finish) cmd_finish ;;
    esac
}

main "$@"
