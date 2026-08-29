#!/usr/bin/env bash
#
# Rebuild the CMO integration branch on top of the current upstream release.
#
# Takes the latest upstream RELEASE tag, replays the patches listed in
# patches.yml on top of it, regenerates the build artefacts, runs the test
# suite, and — only when everything is green — moves the integration branch
# and publishes an immutable tag.
#
# The branch is rebuilt, never merged into: it is force-pushed by design and
# the tags (v<release>-cmo.<n>) are the stable references.
#
# See README.md for the quick start.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$SCRIPT_DIR/lib.sh"

CONFIG_FILE="${SYNC_CONFIG:-$REPO_ROOT/patches.yml}"
DO_PUSH=0
CHECK_ONLY=0
SKIP_TESTS=0
KEEP_WORKTREE=0
WORKTREE=""

usage() {
    cat <<'EOF'
Usage: sync-upstream-release.sh [options]

Rebuilds the integration branch on the latest upstream release and, with
--push, publishes it as a branch update plus an immutable tag.

Options:
  --check         Only report whether a sync is needed (no build, no tests).
                  Exit 0 = up to date, 10 = work to do, >1 = error.
  --push          Publish the result: force-push the integration branch and
                  push the new tag. Without it everything stays local.
  --skip-tests    Build but do not run the test suite. Refuses to combine
                  with --push: an unverified tag is exactly what we avoid.
  --config PATH   Config file (default: patches.yml next to this script's repo
                  root, override with SYNC_CONFIG).
  --keep          Leave the temporary worktree behind for inspection.
  -h, --help      This text.

Environment:
  GITHUB_TOKEN    Optional. Only lifts the anonymous GitHub API rate limit;
                  pushing uses whatever credentials git is configured with.

Exit codes:
  0   done (or already up to date)
  10  --check: a sync is needed
  1   error — conflict that is not a generated file, red tests, missing tools
EOF
}

cleanup() {
    if [ -n "$WORKTREE" ] && [ "$KEEP_WORKTREE" -eq 0 ] && [ -d "$WORKTREE" ]; then
        git -C "$REPO_ROOT" worktree remove --force "$WORKTREE" >/dev/null 2>&1 || true
        git -C "$REPO_ROOT" worktree prune >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

while [ $# -gt 0 ]; do
    case "$1" in
        --check)      CHECK_ONLY=1 ;;
        --push)       DO_PUSH=1 ;;
        --skip-tests) SKIP_TESTS=1 ;;
        --keep)       KEEP_WORKTREE=1 ;;
        --config)     shift; CONFIG_FILE="${1:-}" ;;
        -h|--help)    usage; exit 0 ;;
        *)            usage >&2; die "unknown argument: $1" ;;
    esac
    shift
done

[ -f "$CONFIG_FILE" ] || die "config not found: $CONFIG_FILE"
[ "$SKIP_TESTS" -eq 1 ] && [ "$DO_PUSH" -eq 1 ] && die "--skip-tests cannot be combined with --push"

require_cmd git python3 curl
python3 -c 'import yaml' 2>/dev/null || die "python3 needs PyYAML (pip install pyyaml)"

UPSTREAM_REPO="$(cfg upstream.repo)"
UPSTREAM_URL="$(cfg upstream.url)"
FORK_REMOTE="$(cfg fork.remote)"
INTEGRATION_BRANCH="$(cfg fork.integration_branch)"
TAG_SUFFIX="$(cfg fork.tag_suffix)"
RUN_PYTEST="$(cfg verify.pytest)"

# ---------------------------------------------------------------- discovery

info "fetching $FORK_REMOTE and upstream"
git -C "$REPO_ROOT" remote get-url upstream >/dev/null 2>&1 \
    || git -C "$REPO_ROOT" remote add upstream "$UPSTREAM_URL"
git -C "$REPO_ROOT" fetch --quiet --tags "$FORK_REMOTE"
git -C "$REPO_ROOT" fetch --quiet --tags upstream

RELEASE_TAG="$(latest_release_tag "$UPSTREAM_URL")"
[ -n "$RELEASE_TAG" ] || die "could not determine the latest upstream release"
git -C "$REPO_ROOT" rev-parse --verify --quiet "refs/tags/$RELEASE_TAG^{commit}" >/dev/null \
    || die "upstream release tag $RELEASE_TAG is not in this clone after fetch"
RELEASE_SHA="$(git -C "$REPO_ROOT" rev-parse "refs/tags/$RELEASE_TAG^{commit}")"
ok "latest upstream release: $RELEASE_TAG ($(git -C "$REPO_ROOT" log -1 --format=%h "$RELEASE_SHA"))"

# Highest existing -<suffix>.<n> tag for this release, if any.
PREV_TAG="$(git -C "$REPO_ROOT" tag --list "${RELEASE_TAG}-${TAG_SUFFIX}.*" \
            | sed "s/.*-${TAG_SUFFIX}\.//" | sort -n | tail -1)"
if [ -n "$PREV_TAG" ]; then
    PREV_N="$PREV_TAG"
    PREV_TAG="${RELEASE_TAG}-${TAG_SUFFIX}.${PREV_N}"
    ok "existing tag for this release: $PREV_TAG"
else
    PREV_N=0
    ok "no tag for this release yet"
fi

if [ "$CHECK_ONLY" -eq 1 ]; then
    if [ "$PREV_N" -eq 0 ]; then
        log "RESULT: sync-needed release=$RELEASE_TAG reason=no-tag-for-release"
        exit 10
    fi
    log "RESULT: maybe-up-to-date release=$RELEASE_TAG tag=$PREV_TAG"
    log "(a full run still rebuilds and compares trees — patches may have changed)"
    exit 0
fi

# ------------------------------------------------------------------- build

WORKTREE="$(mktemp -d "${TMPDIR:-/tmp}/pegaprox-sync.XXXXXX")"
rmdir "$WORKTREE"
info "building in $WORKTREE"
git -C "$REPO_ROOT" -c advice.detachedHead=false worktree add --quiet --detach "$WORKTREE" "$RELEASE_SHA"

# Plain counters and newline-separated lists: an empty array under `set -u` is
# a portability minefield (bash 3.2 treats it as unset), and these only ever
# feed the report and the tag message.
N_APPLIED=0
N_SKIPPED=0
APPLIED_LIST=""
SKIPPED_LIST=""

apply_commit() {   # apply_commit <sha> <patch-name>
    local sha="$1" name="$2" conflicts unresolved=0 path rebuild

    if git -C "$WORKTREE" cherry-pick --quiet "$sha" >/dev/null 2>&1; then
        return 0
    fi

    conflicts="$(git -C "$WORKTREE" diff --name-only --diff-filter=U)"

    # No conflict and nothing staged: the change is already in the release.
    if [ -z "$conflicts" ] && git -C "$WORKTREE" diff --cached --quiet; then
        git -C "$WORKTREE" cherry-pick --skip >/dev/null 2>&1 || true
        warn "  $name: $(git -C "$REPO_ROOT" log -1 --format=%h "$sha") already in the release, skipped"
        return 0
    fi

    # Conflicts only in generated files are resolved by regenerating them.
    while IFS= read -r path; do
        [ -n "$path" ] || continue
        local known=0
        while IFS=$'\x1f' read -r gpath _; do
            [ "$path" = "$gpath" ] && known=1
        done < <(cfg_generated)
        [ "$known" -eq 1 ] || { warn "  conflict in $path (not a generated file)"; unresolved=1; }
    done <<< "$conflicts"

    if [ "$unresolved" -eq 1 ]; then
        git -C "$WORKTREE" cherry-pick --abort >/dev/null 2>&1 || true
        die "$name: conflict needs a human — $(echo "$conflicts" | tr '\n' ' ')"
    fi

    while IFS=$'\x1f' read -r path rebuild; do
        [ -n "$path" ] || continue
        echo "$conflicts" | grep -qx "$path" || continue
        git -C "$WORKTREE" checkout --theirs -- "$path" 2>/dev/null || true
        ( cd "$WORKTREE" && bash "$rebuild" >/dev/null 2>&1 ) \
            || die "$name: regenerating $path via $rebuild failed"
        git -C "$WORKTREE" add -- "$path"
    done < <(cfg_generated)

    GIT_EDITOR=true git -C "$WORKTREE" cherry-pick --continue >/dev/null 2>&1 \
        || die "$name: could not continue after regenerating the artefacts"
    return 0
}

while IFS=$'\x1f' read -r name branch base pr summary; do
    [ -n "$name" ] || continue
    info "patch: $name — $summary"

    if ! git -C "$REPO_ROOT" rev-parse --verify --quiet "$FORK_REMOTE/$branch" >/dev/null; then
        die "$name: branch $FORK_REMOTE/$branch not found"
    fi
    merge_base="$(git -C "$REPO_ROOT" merge-base "upstream/$base" "$FORK_REMOTE/$branch")" \
        || die "$name: no merge base between upstream/$base and $FORK_REMOTE/$branch"

    commits="$(git -C "$REPO_ROOT" rev-list --reverse "$merge_base..$FORK_REMOTE/$branch")"
    if [ -z "$commits" ]; then
        warn "  no commits on $branch beyond upstream/$base — skipped"
        SKIPPED_LIST="${SKIPPED_LIST}${name} (no commits beyond upstream/${base})"$'\n'
        N_SKIPPED=$((N_SKIPPED + 1))
        continue
    fi

    # Has the release already got this work? `git cherry` compares patch-ids,
    # so it recognises our commits even when the maintainer rebased them. This
    # is the reliable signal and needs no network; the PR lookup below only
    # adds the squash-merge case, which patch-ids cannot see.
    if [ -z "$(git -C "$REPO_ROOT" cherry "$RELEASE_SHA" "$FORK_REMOTE/$branch" "$merge_base" | grep '^+' || true)" ]; then
        ok "  every commit is already in $RELEASE_TAG — dropping this patch"
        SKIPPED_LIST="${SKIPPED_LIST}${name} (already contained in ${RELEASE_TAG})"$'\n'
        N_SKIPPED=$((N_SKIPPED + 1))
        continue
    fi

    if [ -n "$pr" ]; then
        merged="$(pr_merged "$UPSTREAM_REPO" "$pr")"
        case "$merged" in
            true)
                ok "  upstream PR #$pr is merged — dropping this patch from the set"
                SKIPPED_LIST="${SKIPPED_LIST}${name} (PR #${pr} merged upstream)"$'\n'
                N_SKIPPED=$((N_SKIPPED + 1))
                continue ;;
            unknown)
                warn "  PR #$pr status unavailable (API unreachable) — relying on patch-ids" ;;
        esac
    fi

    n=0
    while IFS= read -r sha; do
        [ -n "$sha" ] || continue
        apply_commit "$sha" "$name"
        n=$((n + 1))
    done <<< "$commits"
    ok "  applied $n commit(s)"
    APPLIED_LIST="${APPLIED_LIST}${name}: ${summary}"$'\n'
    N_APPLIED=$((N_APPLIED + 1))
done < <(cfg_patches)

# Artefacts are regenerated once more at the end: later patches may have
# touched sources after an earlier conflict was resolved by a rebuild.
while IFS=$'\x1f' read -r path rebuild; do
    [ -n "$path" ] || continue
    info "regenerating $path"
    ( cd "$WORKTREE" && bash "$rebuild" >/dev/null 2>&1 ) || die "$rebuild failed"
done < <(cfg_generated)

if ! git -C "$WORKTREE" diff --quiet; then
    info "artefacts changed after the final rebuild — committing"
    git -C "$WORKTREE" add -A
    git -C "$WORKTREE" commit --quiet -m "build: regenerate artefacts for the backported fixes

Generated from the sources on top of $RELEASE_TAG; the upstream bundle is
built from a different tree, so it differs after the cherry-picks."
fi

BUILT_SHA="$(git -C "$WORKTREE" rev-parse HEAD)"
BUILT_TREE="$(git -C "$WORKTREE" rev-parse 'HEAD^{tree}')"
ok "built $(git -C "$WORKTREE" log -1 --format=%h) on $RELEASE_TAG"

# ------------------------------------------------------------------ verify

if [ "$SKIP_TESTS" -eq 0 ] && { [ "$RUN_PYTEST" = "True" ] || [ "$RUN_PYTEST" = "true" ]; }; then
    info "running the test suite (isolated venv, like upstream CI)"
    (
        cd "$WORKTREE"
        python3 -m venv .venv
        .venv/bin/pip install --quiet --upgrade pip
        .venv/bin/pip install --quiet -r requirements.txt -r requirements-dev.txt
        .venv/bin/python -m pytest tests/ -q
    ) || die "test suite failed on the built tree — nothing published"
    ok "test suite passed"
else
    warn "test suite skipped"
fi

# --------------------------------------------------------------- publish

if [ "$PREV_N" -gt 0 ]; then
    prev_tree="$(git -C "$REPO_ROOT" rev-parse "refs/tags/${PREV_TAG}^{tree}" 2>/dev/null || echo none)"
    if [ "$prev_tree" = "$BUILT_TREE" ]; then
        ok "result is identical to $PREV_TAG — nothing to publish"
        log "RESULT: up-to-date release=$RELEASE_TAG tag=$PREV_TAG"
        exit 0
    fi
    warn "result differs from $PREV_TAG (patches changed) — publishing a new revision"
fi

NEW_N=$((PREV_N + 1))
NEW_TAG="${RELEASE_TAG}-${TAG_SUFFIX}.${NEW_N}"

tag_body="PegaProx ${RELEASE_TAG} + CMO fixes

Upstream release ${RELEASE_TAG} ($(git -C "$REPO_ROOT" log -1 --format=%h "$RELEASE_SHA")) with our not-yet-upstream fixes applied:

$(printf '%s' "$APPLIED_LIST" | sed '/^$/d; s/^/  - /')"
if [ "$N_SKIPPED" -gt 0 ]; then
    tag_body="$tag_body

Dropped from the patch set:

$(printf '%s' "$SKIPPED_LIST" | sed '/^$/d; s/^/  - /')"
fi
tag_body="$tag_body

Built by scripts/sync-upstream-release.sh; the test suite passed on this tree."

git -C "$WORKTREE" tag -a "$NEW_TAG" -m "$tag_body" "$BUILT_SHA"
git -C "$REPO_ROOT" branch --force "$INTEGRATION_BRANCH" "$BUILT_SHA"
ok "local branch $INTEGRATION_BRANCH and tag $NEW_TAG created"

if [ "$DO_PUSH" -eq 1 ]; then
    info "publishing"
    git -C "$REPO_ROOT" push --force-with-lease "$FORK_REMOTE" "$INTEGRATION_BRANCH"
    git -C "$REPO_ROOT" push "$FORK_REMOTE" "$NEW_TAG"
    ok "pushed $INTEGRATION_BRANCH and $NEW_TAG"
    log "RESULT: published release=$RELEASE_TAG tag=$NEW_TAG commit=$(git -C "$REPO_ROOT" rev-parse --short "$BUILT_SHA") applied=$N_APPLIED dropped=$N_SKIPPED"
else
    warn "not pushed (no --push) — branch and tag exist locally only"
    log "RESULT: built release=$RELEASE_TAG tag=$NEW_TAG commit=$(git -C "$REPO_ROOT" rev-parse --short "$BUILT_SHA") applied=$N_APPLIED dropped=$N_SKIPPED"
fi
