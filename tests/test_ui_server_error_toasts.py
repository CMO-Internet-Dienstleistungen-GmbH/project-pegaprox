# -*- coding: utf-8 -*-
"""Regression guards for server-provided errors in snapshot toasts."""

import os
import shutil
import subprocess

import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API_ERRORS = os.path.join(REPO_ROOT, 'web', 'src', 'api_errors.js')
VM_MODALS = os.path.join(REPO_ROOT, 'web', 'src', 'vm_modals.js')
BUILD_SCRIPT = os.path.join(REPO_ROOT, 'web', 'Dev', 'build.sh')


def test_api_error_message_prefers_server_reason_and_keeps_fallback():
    """The shipped JavaScript helper preserves the API error contract."""
    node = shutil.which('node')
    if not node:
        pytest.skip('node is required to execute the frontend error helper')

    script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const sandbox = {};
vm.runInNewContext(source + '\n;globalThis.__message = PegaProxApiErrors.message;', sandbox);
(async () => {
    const message = sandbox.__message;
    if (await message({json: async () => ({error: 'Invalid snapshot name'})}, 'Snapshot failed') !== 'Invalid snapshot name') process.exit(1);
    if (await message({json: async () => { throw new Error('not JSON'); }}, 'Snapshot failed') !== 'Snapshot failed') process.exit(2);
    if (await message(null, 'Snapshot failed') !== 'Snapshot failed') process.exit(3);
})().catch(() => process.exit(4));
"""
    result = subprocess.run(
        [node, '-e', script, API_ERRORS], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr


def test_all_snapshot_mutations_use_the_server_error_reader():
    source = open(VM_MODALS, encoding='utf-8').read()

    assert "else addToast?.('Snapshot failed', 'error')" not in source
    assert source.count('PegaProxApiErrors.message(') >= 5


def test_error_reader_is_loaded_before_snapshot_components():
    source = open(BUILD_SCRIPT, encoding='utf-8').read()

    assert source.index('api_errors.js') < source.index('vm_modals.js')
