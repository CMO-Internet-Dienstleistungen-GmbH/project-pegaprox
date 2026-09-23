# -*- coding: utf-8 -*-
"""Runs the jsdom behaviour tests in web/test/ from the Python suite.

The frontend has no test runner of its own, and this patch deliberately does not
touch .github/workflows/. This wrapper is how web/test/ reaches CI anyway: pytest
collects it like any other test, so `python -m pytest` runs the JS guards too.

It SKIPS rather than fails when node or the npm dependencies are unavailable — a
Python-only checkout must not go red because a JavaScript toolchain is missing.
The tests themselves live in web/test/request-amplification.test.js; see
web/test/README.md for what they assert and why.
"""

import os
import shutil
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_TEST_DIR = os.path.join(REPO_ROOT, 'web', 'test')
TEST_FILE = 'request-amplification.test.js'

NPM_INSTALL_TIMEOUT = 300
NODE_TEST_TIMEOUT = 300


def _node_major(node):
    """Major version of the node binary, or None if it cannot be determined."""
    try:
        out = subprocess.run([node, '--version'], capture_output=True, text=True,
                             timeout=30, check=True).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return None
    try:
        return int(out.lstrip('v').split('.')[0])
    except (ValueError, IndexError):
        return None


@pytest.fixture(scope='module')
def node_bin():
    """A node binary new enough for the built-in test runner, with deps installed."""
    node = shutil.which('node')
    if not node:
        pytest.skip('node not installed — frontend behaviour tests skipped')

    major = _node_major(node)
    if major is None or major < 18:
        pytest.skip(f'node >= 18 required for --test (found {major}) — skipped')

    if not os.path.isdir(os.path.join(WEB_TEST_DIR, 'node_modules')):
        npm = shutil.which('npm')
        if not npm:
            pytest.skip('npm not installed and web/test/node_modules missing — skipped')
        try:
            res = subprocess.run(
                [npm, 'install', '--no-audit', '--no-fund'],
                cwd=WEB_TEST_DIR, capture_output=True, text=True,
                timeout=NPM_INSTALL_TIMEOUT,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            pytest.skip(f'npm install failed ({exc}) — skipped')
        if res.returncode != 0:
            # No network in a sandboxed build is the normal reason. Not a failure
            # of the code under test, so don't report it as one.
            pytest.skip(f'npm install failed (rc={res.returncode}) — skipped')

    return node


def test_frontend_request_amplification_guards(node_bin):
    """web/test/request-amplification.test.js must pass.

    It asserts that getAuthHeaders keeps one identity across provider renders,
    that ClusterHealthBadge issues a single request across many re-renders when
    authFetch is stable, and that the guest-agent IP cache stores a "no address"
    answer as a result instead of re-requesting the guest on every pass.
    """
    res = subprocess.run(
        [node_bin, '--test', TEST_FILE],
        cwd=WEB_TEST_DIR, capture_output=True, text=True, timeout=NODE_TEST_TIMEOUT,
    )
    if res.returncode != 0:
        pytest.fail(
            'frontend behaviour tests failed\n'
            f'--- stdout ---\n{res.stdout}\n--- stderr ---\n{res.stderr}'
        )


def test_the_guest_agent_is_not_asked_about_a_guest_that_has_none():
    """The guest-info fan-out skips a row that is not a Proxmox guest.

    Read out of the source rather than rendered, because the filter lives inside a
    component this suite has no way to mount. It is still worth holding: the route
    answers for a Proxmox cluster and nothing else, so on a source whose guests have no
    QEMU agent every visible row costs a round trip whose only possible answer is "no
    address" — and it costs it again on every render. A guest from such a source carries
    the id its own hypervisor knows it by, which is what the guard reads.
    """
    tables = os.path.join(REPO_ROOT, 'web', 'src', 'tables.js')
    with open(tables, encoding='utf-8') as handle:
        source = handle.read()

    marker = "fetch(`/api/clusters/${cid}/vms/${vm.node}/qemu/${vm.vmid}/guest-info`"
    assert marker in source, 'the guest-info fetch moved — this guard needs rechecking'

    head = source[:source.index(marker)]
    selection = head[head.rindex('const toFetch = paginatedResources.filter'):]
    assert '!r.hyperv_guid' in selection, (
        'the guest-info selection no longer skips guests that have no QEMU agent; '
        'every visible row on such a source is one round trip per render')
