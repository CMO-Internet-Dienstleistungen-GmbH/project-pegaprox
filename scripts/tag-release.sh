#!/usr/bin/env bash
#
# Tags the integration branch that a publish pushed but could not tag.
#
# The routine that runs the sync builds, tests and pushes cmo/main, but the
# sandbox it runs in may not push tags -- and the tag is what gets deployed and
# rolled back to, so it is a maintainer's to set anyway. This is that one step,
# run in a local clone: it checks what the remote has, refuses anything that
# was not tested, and sets and pushes the next <release>-<suffix>.<n> tag.
#
# Everything is read from the remote, nothing from files the routine wrote: the
# release from upstream's tags, the build from <remote>/<integration branch>,
# the test result from the fork's CI, the patch list from the commit subjects
# matched against patches.yml.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$SCRIPT_DIR/lib.sh"
# Read by cfg() in lib.sh.
# shellcheck disable=SC2034
CONFIG_FILE="${SYNC_CONFIG:-$REPO_ROOT/patches.yml}"

DRY_RUN=0
case "${1:-}" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help)
        cat <<'EOF'
Usage: tag-release.sh [--dry-run]

Sets and pushes the tag for the integration branch the last publish pushed.
Refuses when the branch already carries a tag, does not sit on the latest
upstream release, or has no green CI run. --dry-run prints the tag and its
message without creating anything.
EOF
        exit 0 ;;
    "") : ;;
    *) die "unknown option: $1" ;;
esac

require_cmd git python3 gh
python3 -c 'import yaml' 2>/dev/null || die "python3 needs PyYAML (pip install pyyaml)"

UPSTREAM_URL="$(cfg upstream.url)"
FORK_REMOTE="$(cfg fork.remote)"
INTEGRATION_BRANCH="$(cfg fork.integration_branch)"
TAG_SUFFIX="$(cfg fork.tag_suffix)"
CI_WORKFLOW="$(cfg verify.ci.workflow)"

info "fetching $FORK_REMOTE and upstream"
git -C "$REPO_ROOT" remote get-url upstream >/dev/null 2>&1 \
    || git -C "$REPO_ROOT" remote add upstream "$UPSTREAM_URL"
git -C "$REPO_ROOT" fetch --quiet --tags "$FORK_REMOTE"
git -C "$REPO_ROOT" fetch --quiet --tags upstream

RELEASE_TAG="$(latest_release_tag "$UPSTREAM_URL")"
[ -n "$RELEASE_TAG" ] || die "could not determine the latest upstream release"
RELEASE_SHA="$(git -C "$REPO_ROOT" rev-parse "refs/tags/$RELEASE_TAG^{commit}")"
BRANCH_SHA="$(git -C "$REPO_ROOT" rev-parse "refs/remotes/$FORK_REMOTE/$INTEGRATION_BRANCH")"

existing="$(git -C "$REPO_ROOT" tag --points-at "$BRANCH_SHA" --list "*-${TAG_SUFFIX}.*" | head -1)"
if [ -n "$existing" ]; then
    ok "$FORK_REMOTE/$INTEGRATION_BRANCH is already tagged: $existing — nothing to do"
    exit 0
fi

git -C "$REPO_ROOT" merge-base --is-ancestor "$RELEASE_SHA" "$BRANCH_SHA" \
    || die "$FORK_REMOTE/$INTEGRATION_BRANCH does not sit on the latest release $RELEASE_TAG — run a publish first"
[ -z "$(git -C "$REPO_ROOT" rev-list --merges "$RELEASE_SHA..$BRANCH_SHA")" ] \
    || die "$FORK_REMOTE/$INTEGRATION_BRANCH has merge commits above $RELEASE_TAG — it was not built by the sync"
ok "$FORK_REMOTE/$INTEGRATION_BRANCH: $(git -C "$REPO_ROOT" log -1 --format='%h %s' "$BRANCH_SHA")"

# The gate: a green run of the workflow on exactly this commit. A branch that
# was moved by hand has none, and is not tagged.
FORK_SLUG="$(git -C "$REPO_ROOT" remote get-url "$FORK_REMOTE" \
             | sed -E 's#^(git@github\.com:|https://github\.com/)##; s#\.git$##')"
CI_RUN_URL="$(gh run list -R "$FORK_SLUG" --workflow "$CI_WORKFLOW" --event pull_request \
              --commit "$BRANCH_SHA" --status success --limit 1 --json url --jq '.[0].url // empty')"
[ -n "$CI_RUN_URL" ] \
    || die "no green $CI_WORKFLOW run for $(git -C "$REPO_ROOT" rev-parse --short "$BRANCH_SHA") — not tagging an untested build"
ok "CI passed: $CI_RUN_URL"

prev_n="$(git -C "$REPO_ROOT" tag --list "${RELEASE_TAG}-${TAG_SUFFIX}.*" \
          | sed "s/.*-${TAG_SUFFIX}\.//" | sort -n | tail -1)"
NEW_TAG="${RELEASE_TAG}-${TAG_SUFFIX}.$(( ${prev_n:-0} + 1 ))"

# One line per patch commit, in the wording a publish uses: the subject is the
# patch's stable commit_message, which names its entry in patches.yml.
applied="$(git -C "$REPO_ROOT" log --reverse --format=%s "$RELEASE_SHA..$BRANCH_SHA" \
           | python3 -c '
import sys, yaml
with open(sys.argv[1]) as fh:
    patches = (yaml.safe_load(fh) or {}).get("patches") or []
by_subject = {str(p.get("commit_message") or ""): p for p in patches}
for subject in sys.stdin.read().splitlines():
    if subject.startswith("build: regenerate artefacts"):
        continue
    p = by_subject.get(subject)
    if p is None:
        print(f"  - {subject}")
    elif (p.get("kind") or "fix") == "internal":
        print("  - %s: %s [internal — stays in the fork]" % (p["name"], p.get("summary") or ""))
    else:
        print("  - %s: %s" % (p["name"], p.get("summary") or ""))
' "$CONFIG_FILE")"

tag_body="PegaProx ${RELEASE_TAG} + CMO fixes

Upstream release ${RELEASE_TAG} ($(git -C "$REPO_ROOT" log -1 --format=%h "$RELEASE_SHA")) with our not-yet-upstream fixes applied:

${applied}

Built by scripts/sync-upstream-release.sh; the test suite passed on this tree in CI:
${CI_RUN_URL}"

info "tag $NEW_TAG on $(git -C "$REPO_ROOT" rev-parse --short "$BRANCH_SHA")"
printf '%s\n' "$tag_body" | sed 's/^/    /'

if [ "$DRY_RUN" -eq 1 ]; then
    log "RESULT: dry-run tag=$NEW_TAG commit=$(git -C "$REPO_ROOT" rev-parse --short "$BRANCH_SHA")"
    exit 0
fi

git -C "$REPO_ROOT" tag -a "$NEW_TAG" -m "$tag_body" "$BRANCH_SHA"
if ! git -C "$REPO_ROOT" push "$FORK_REMOTE" "refs/tags/$NEW_TAG"; then
    git -C "$REPO_ROOT" tag -d "$NEW_TAG" >/dev/null
    die "pushing $NEW_TAG failed — the local tag was removed again, so the counter stays honest"
fi
ok "pushed $NEW_TAG"
log "RESULT: tagged tag=$NEW_TAG commit=$(git -C "$REPO_ROOT" rev-parse --short "$BRANCH_SHA")"
