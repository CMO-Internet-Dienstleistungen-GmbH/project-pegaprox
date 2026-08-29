#!/usr/bin/env bash
#
# Entry point for the CMO fork automation. Chains the steps in the order that
# works and adds a verify that asks the repository, not the tool.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$SCRIPT_DIR/lib.sh"

# Read by cfg() in lib.sh.
# shellcheck disable=SC2034
CONFIG_FILE="${SYNC_CONFIG:-$REPO_ROOT/patches.yml}"

usage() {
    cat <<'EOF'
Usage: run.sh <command>

Commands:
  check     Is there a new upstream release we have no tag for?
            Exit 0 = nothing to do, 10 = sync needed.
  sync      Rebuild the integration branch locally and run the tests.
            Publishes nothing.
  publish   Same as sync, but force-pushes the integration branch and pushes
            the new tag once the tests are green.
  verify    Ask the fork what is actually published: the newest -cmo tag, the
            commit it points at, whether the integration branch matches it,
            and which upstream release it sits on.
  -h        This text.

Typical use:
  ./scripts/run.sh check && ./scripts/run.sh publish
EOF
}

cmd_verify() {
    local remote branch suffix
    remote="$(cfg fork.remote)"
    branch="$(cfg fork.integration_branch)"
    suffix="$(cfg fork.tag_suffix)"

    git -C "$REPO_ROOT" fetch --quiet --tags "$remote"

    local newest
    newest="$(git -C "$REPO_ROOT" tag --list "*-${suffix}.*" --sort=-creatordate | head -1)"
    [ -n "$newest" ] || die "no -${suffix} tag exists yet"

    local tag_sha branch_sha base_tag
    tag_sha="$(git -C "$REPO_ROOT" rev-parse "${newest}^{commit}")"
    branch_sha="$(git -C "$REPO_ROOT" rev-parse "refs/remotes/$remote/$branch" 2>/dev/null || echo missing)"
    base_tag="${newest%-"${suffix}".*}"

    log "newest tag:          $newest"
    log "  commit:            $(git -C "$REPO_ROOT" log -1 --format='%h %s' "$tag_sha")"
    log "  upstream base:     $base_tag"
    log "  patches on top:    $(git -C "$REPO_ROOT" rev-list --count "refs/tags/$base_tag..$tag_sha" 2>/dev/null || echo '?')"
    log "$remote/$branch:      $(git -C "$REPO_ROOT" log -1 --format='%h %s' "$branch_sha" 2>/dev/null || echo missing)"

    if [ "$tag_sha" = "$branch_sha" ]; then
        ok "integration branch and newest tag agree"
    else
        warn "integration branch does NOT point at the newest tag"
        return 1
    fi

    local latest_release
    latest_release="$(latest_release_tag "$(cfg upstream.repo)")"
    if [ "$latest_release" = "$base_tag" ]; then
        ok "built on the current upstream release ($latest_release)"
    else
        warn "upstream has moved on: latest release is $latest_release, we are on $base_tag"
        return 10
    fi
}

case "${1:-}" in
    check)   exec "$SCRIPT_DIR/sync-upstream-release.sh" --check ;;
    sync)    exec "$SCRIPT_DIR/sync-upstream-release.sh" ;;
    publish) exec "$SCRIPT_DIR/sync-upstream-release.sh" --push ;;
    verify)  cmd_verify ;;
    -h|--help|"") usage ;;
    *)       usage >&2; die "unknown command: $1" ;;
esac
