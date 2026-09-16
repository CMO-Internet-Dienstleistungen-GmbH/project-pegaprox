"""The wizard must preselect what the server would accept.

Which virtio-win release a guest may be given is decided twice: in `hyperv_drivers`, which
the preflight and the injection ask, and in `web/src/hyperv.js`, which fills the dropdown.
Two copies of one rule drift, and the way this one would drift is invisible — the wizard
would preselect an ISO that the preflight then blocks, or worse, one it accepts for the
wrong reason.

So both are asked the same questions here, and the answers have to match.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from pegaprox.core import hyperv_drivers as drivers

HELPERS = Path('web/src/hyperv.js')

#: The same node contents both sides are asked about.
ON_NODE = [
    {'volid': 'vm-pool:iso/gparted-live-1.7.0-8-amd64.iso', 'release': None},
    {'volid': 'vm-pool:iso/debian-amd64-netinst-3cx.iso', 'release': None},
    {'volid': 'vm-pool:iso/virtio-win-0.1.189.iso', 'release': '0.1.189'},
]
WITH_MODERN = ON_NODE + [{'volid': 'vm-pool:iso/virtio-win-0.1.262.iso', 'release': '0.1.262'}]
CATALOGUE = [{'release': release} for release in sorted(drivers.CATALOGUE)]


def _javascript_answers():
    """Run the wizard's own helpers in node and return what they decided."""
    node = shutil.which('node')
    if not node:
        pytest.skip('node is not available')
    if not HELPERS.exists():
        pytest.skip(f'{HELPERS} is not in this checkout')

    script = f'''
const fs = require('fs');
const src = fs.readFileSync({str(HELPERS)!r}, 'utf8');
const start = src.indexOf('const HV_LEGACY_BUILD');
const end = src.indexOf('// \\u2500'.repeat(1), start);
eval(src.slice(start, end));
const onNode = {json.dumps(ON_NODE)};
const withModern = {json.dumps(WITH_MODERN)};
const catalogue = {json.dumps(CATALOGUE)};
const pick = (b, list) => {{ const r = hvIsoForBuild(b, list); return r ? r.volid : null; }};
console.log(JSON.stringify({{
  legacy_on_node: pick({drivers.LEGACY_BUILD}, onNode),
  modern_with_only_legacy: pick(20348, onNode),
  modern_with_current: pick(20348, withModern),
  fetch_legacy: hvReleaseToFetch({drivers.LEGACY_BUILD}, catalogue),
  fetch_modern: hvReleaseToFetch(20348, catalogue),
  build_from_plan: hvGuestBuild({{guest_images: [{{windows: true, build: 20348}}]}}),
}}));
'''
    result = subprocess.run([node, '-e', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_both_sides_preselect_the_same_iso():
    js = _javascript_answers()

    assert js['legacy_on_node'] == drivers.preferred_iso(drivers.LEGACY_BUILD, ON_NODE)
    assert js['modern_with_current'] == drivers.preferred_iso(20348, WITH_MODERN)
    # The one that matters: 0.1.189 has no directory for a current guest, so neither side
    # may offer it — the wizard falls through to a download instead.
    assert js['modern_with_only_legacy'] is None
    assert drivers.preferred_iso(20348, ON_NODE) is None


def test_both_sides_offer_the_same_download():
    js = _javascript_answers()

    assert js['fetch_legacy'] == drivers.release_to_fetch(drivers.LEGACY_BUILD)
    assert js['fetch_modern'] == drivers.release_to_fetch(20348)


def test_the_wizard_reads_the_build_out_of_the_plan():
    assert _javascript_answers()['build_from_plan'] == 20348
