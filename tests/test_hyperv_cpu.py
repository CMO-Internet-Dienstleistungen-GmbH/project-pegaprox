# Fork issue #15 — the CPU type an imported VM is created with.
#
# Proxmox falls back to kvm64 when the create call names none. The import suggests
# x86-64-v3, and x86-64-v2-AES on a node whose processor cannot provide v3: a VM given a
# model its host lacks does not start. The flags come from /nodes/{node}/status, in the
# shape a PVE 9.2 node returns them (`cpuinfo.flags`, one space-separated string).

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from pegaprox.core import hyperv_cpu

V3_FLAGS = ('fpu sse sse2 ssse3 sse4_1 sse4_2 popcnt aes avx avx2 bmi1 bmi2 f16c fma abm '
            'movbe xsave')


class _Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _Target:
    host = 'target-host.invalid'
    api_port = 8006
    is_connected = True
    cluster_type = 'proxmox'

    def __init__(self, flags=V3_FLAGS, status_code=200, raises=False):
        self._flags, self._status, self._raises = flags, status_code, raises
        self.asked = []

    def _api_get(self, url):
        self.asked.append(url)
        if self._raises:
            raise ConnectionError('node unreachable')
        return _Response(self._status, {'data': {'cpuinfo': {
            'model': 'a processor', 'cpus': 48, 'flags': self._flags}}})


class TestTheSuggestion:
    def test_a_processor_with_every_v3_flag_gets_v3(self):
        assert hyperv_cpu.default_cpu_type(set(V3_FLAGS.split())) == 'x86-64-v3'

    @pytest.mark.parametrize('missing', sorted(hyperv_cpu.X86_64_V3_FLAGS))
    def test_one_missing_v3_flag_falls_back_to_v2_aes(self, missing):
        flags = set(V3_FLAGS.split()) - {missing}
        assert hyperv_cpu.default_cpu_type(flags) == 'x86-64-v2-AES'

    def test_flags_nobody_could_read_fall_back_to_v2_aes(self):
        """A slower guest is the cheaper mistake; a VM that does not start is not."""
        assert hyperv_cpu.default_cpu_type(None) == 'x86-64-v2-AES'

    def test_the_flags_are_read_from_the_node_status(self):
        target = _Target()
        assert hyperv_cpu.node_cpu_flags(target, 'node-a') == set(V3_FLAGS.split())
        assert target.asked == ['https://target-host.invalid:8006/api2/json/nodes/node-a/status']

    @pytest.mark.parametrize('target', [_Target(status_code=500), _Target(raises=True),
                                        _Target(flags='')])
    def test_a_node_that_does_not_say_has_no_flags(self, target):
        assert hyperv_cpu.node_cpu_flags(target, 'node-a') is None


class TestTheChoice:
    def test_an_offered_type_is_taken_as_chosen(self):
        cpu, note = hyperv_cpu.chosen_cpu_type({'cpu_type': 'host'}, _Target(), 'node-a')
        assert cpu == 'host'
        assert 'as chosen' in note

    def test_no_choice_is_the_nodes_suggestion(self):
        assert hyperv_cpu.chosen_cpu_type({}, _Target(), 'node-a')[0] == 'x86-64-v3'
        without_v3 = _Target(flags='sse4_2 aes popcnt')
        assert hyperv_cpu.chosen_cpu_type({}, without_v3, 'node-a')[0] == 'x86-64-v2-AES'

    def test_a_value_outside_the_list_never_reaches_the_create_call(self):
        cpu, note = hyperv_cpu.chosen_cpu_type({'cpu_type': 'host,flags=+pcid'}, _Target(),
                                               'node-a')
        assert cpu == 'x86-64-v3'
        assert 'not one this import offers' in note


class TestTheRoute:
    def test_the_wizard_is_told_the_suggestion_for_the_node(self, api, seed):
        api.set_manager('pve_1', _Target(flags='sse4_2 aes popcnt'))
        admin = seed.user('root', role='admin')

        response = api.as_user(admin).get('/api/hyperv/target-cpu?cluster=pve_1&node=node-a')

        assert response.status_code == 200
        body = response.get_json()
        assert body['default'] == 'x86-64-v2-AES'
        assert body['flags_known'] is True
        assert body['supports_x86_64_v3'] is False
        assert body['types'] == list(hyperv_cpu.CPU_TYPES)

    def test_node_and_cluster_are_required(self, api, seed):
        admin = seed.user('root', role='admin')
        assert api.as_user(admin).get('/api/hyperv/target-cpu?cluster=pve_1').status_code == 400

    def test_an_anonymous_caller_is_refused(self, api):
        assert api.anon().get('/api/hyperv/target-cpu?cluster=pve_1&node=node-a').status_code \
            in (401, 403)


class TestTheWizard:
    SOURCE = Path('web/src/dashboard.js').read_text() if Path('web/src/dashboard.js').exists() else ''
    HELPERS = Path('web/src/hyperv.js')

    def test_the_offered_types_are_the_ones_the_server_accepts(self):
        node = shutil.which('node')
        if not node:
            pytest.skip('node is not available')
        src = self.HELPERS.read_text()
        start = src.index('const HV_CPU_TYPES')
        end = src.index(';', start) + 1
        out = subprocess.run([node, '-e', src[start:end] + 'console.log(JSON.stringify(HV_CPU_TYPES))'],
                             capture_output=True, text=True, check=True).stdout
        assert json.loads(out) == list(hyperv_cpu.CPU_TYPES)

    def test_the_field_asks_the_chosen_node(self):
        assert '/hyperv/target-cpu' in self.SOURCE
        assert 'value={xhmForm.cpu_type}' in self.SOURCE
        assert "cpu_type: ''" in self.SOURCE
