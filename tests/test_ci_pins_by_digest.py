"""What the workflows run is fixed by commit and digest, not by a tag that can move.

apt-repo.yml was the one workflow still on version tags (checkout@v4,
upload-pages-artifact@v3, deploy-pages@v4), in the job that imports the APT signing key
(#978). The Docker workflows pinned their actions, but setup-qemu-action and
setup-buildx-action each pull an image of their own (tonistiigi/binfmt, moby/buildkit) by a
moving tag, in jobs that may push to GHCR (#982). Both are pinned now: every action to a
commit with the tag it was resolved from as a comment, every image those two pull to a digest.
NS Oct 2026
"""
import glob
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOWS = sorted(glob.glob(os.path.join(ROOT, '.github', 'workflows', '*.yml')))
USES = re.compile(r'^\s*(?:-\s+)?uses:\s*(\S+)(.*)$', re.M)
DIGEST = r'@sha256:[0-9a-f]{64}'


def _read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


def _steps(text, action):
    """The text of every step that uses `action`, up to the next step."""
    out = []
    for m in re.finditer(rf'uses:\s*{re.escape(action)}@', text):
        rest = text[m.end():]
        nxt = re.search(r'\n {4,8}- ', rest)
        out.append(rest[:nxt.start()] if nxt else rest)
    return out


def test_there_are_workflows_to_check():
    names = {os.path.basename(p) for p in WORKFLOWS}
    assert {'apt-repo.yml', 'docker.yml', 'docker-testing.yml', 'release-images.yml'} <= names


@pytest.mark.parametrize('path', WORKFLOWS, ids=os.path.basename)
def test_every_action_is_pinned_to_a_commit_with_its_tag(path):
    text = _read(path)
    for ref, rest in USES.findall(text):
        if ref.startswith('./'):
            continue
        action, _, pin = ref.partition('@')
        assert re.fullmatch(r'[0-9a-f]{40}', pin), f'{os.path.basename(path)}: {ref} is not pinned to a commit'
        assert re.match(r'\s+#\s*v\d+\.\d+\.\d+\s*$', rest), \
            f'{os.path.basename(path)}: {action} has no "# vX.Y.Z" comment'


def test_the_apt_job_with_the_signing_key_runs_no_tag():
    text = _read(os.path.join(ROOT, '.github', 'workflows', 'apt-repo.yml'))
    refs = [ref for ref, _ in USES.findall(text)]
    assert {r.split('@')[0] for r in refs} == {'actions/checkout', 'actions/upload-pages-artifact',
                                               'actions/deploy-pages'}
    assert not [r for r in refs if re.search(r'@v\d', r)], refs


@pytest.mark.parametrize('path', WORKFLOWS, ids=os.path.basename)
def test_the_images_of_the_setup_actions_are_pinned_by_digest(path):
    text = _read(path)
    for step in _steps(text, 'docker/setup-qemu-action'):
        assert re.search(rf'\n\s+image:\s*\S+:qemu-v[\d.]+{DIGEST}\s*\n', step + '\n'), step
    for step in _steps(text, 'docker/setup-buildx-action'):
        assert re.search(rf'\n\s+driver-opts:\s*image=\S+:v[\d.]+{DIGEST}\s*\n', step + '\n'), step


def test_the_setup_actions_are_where_the_digests_are_needed():
    """docker.yml and docker-testing.yml use both, the release images the binfmt one."""
    counts = {}
    for path in WORKFLOWS:
        text = _read(path)
        counts[os.path.basename(path)] = (len(_steps(text, 'docker/setup-qemu-action')),
                                          len(_steps(text, 'docker/setup-buildx-action')))
    assert counts['docker.yml'] == (1, 1)
    assert counts['docker-testing.yml'] == (1, 1)
    assert counts['release-images.yml'][0] == 1


def test_one_digest_per_image_across_the_workflows():
    """The same image at the same digest everywhere, so a bump cannot leave one behind."""
    seen = {}
    for path in WORKFLOWS:
        for name, digest in re.findall(r'(tonistiigi/binfmt|moby/buildkit):\S+@(sha256:[0-9a-f]{64})', _read(path)):
            seen.setdefault(name, set()).add(digest)
    assert set(seen) == {'tonistiigi/binfmt', 'moby/buildkit'}
    assert all(len(d) == 1 for d in seen.values()), seen


def test_no_long_dash_in_the_workflow_lines_of_this_change():
    for path in WORKFLOWS:
        for line in _read(path).splitlines():
            if '(#978)' in line or '(#982)' in line or '@sha256:' in line:
                assert '\N{EM DASH}' not in line, line
