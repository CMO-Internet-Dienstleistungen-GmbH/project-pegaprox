"""Which virtio-win release may drive which guest.

The failure this prevents is silent. Windows does not report a driver whose signature it
rejects — it does not load it, and a boot-start storage driver that is not loaded stops the
machine at 0xc0000428. Nothing in the migration's own result shows it, which is why the
rule is code with tests rather than a sentence in a runbook.
"""

import pytest

from pegaprox.core import hyperv_drivers as drivers


class TestReadingTheReleaseOffTheName:
    def test_the_usual_spellings(self):
        assert drivers.release_of('local:iso/virtio-win-0.1.189.iso') == '0.1.189'
        assert drivers.release_of('/var/lib/vz/template/iso/virtio-win-0.1.262-2.iso') == '0.1.262'
        assert drivers.release_of('virtio_win_0.1.302.iso') == '0.1.302'

    def test_a_name_that_states_nothing(self):
        assert drivers.release_of('local:iso/virtio-win.iso') is None
        assert drivers.release_of('') is None
        assert drivers.release_of(None) is None


class TestServer2012R2MayOnlyHave0_1_189:
    """Build 9600. From 0.1.221 the drivers are self-signed, and a self-signed boot-start
    driver cannot load on x64 at all."""

    def test_the_required_release_passes(self):
        assert drivers.refuse_iso(9600, 'local:iso/virtio-win-0.1.189.iso') is None

    def test_a_later_release_is_refused_and_says_why(self):
        reason = drivers.refuse_iso(9600, 'local:iso/virtio-win-0.1.262.iso')
        assert reason and '0.1.189' in reason and '0xc0000428' in reason

    def test_an_iso_that_names_no_release_is_refused_too(self):
        # An unnamed release is not evidence of the right one, and injecting on a maybe
        # produces exactly the silent failure this exists to prevent.
        reason = drivers.refuse_iso(9600, 'local:iso/virtio-win.iso')
        assert reason and 'does not say which release' in reason

    def test_the_refusal_names_where_to_get_the_right_one(self):
        reason = drivers.refuse_iso(9600, 'local:iso/virtio-win.iso')
        assert 'virtio-win-0.1.189.iso' in reason

    def test_a_guest_with_no_rule_takes_anything(self):
        assert drivers.refuse_iso(20348, 'local:iso/virtio-win-0.1.262.iso') is None
        assert drivers.refuse_iso(None, 'local:iso/virtio-win.iso') is None


class TestTheGuardRunsOnTheNode:
    """The build number is only known on the node, inside the script that mounted the
    volume. Whether a release satisfies a build is known here, before anything runs."""

    def test_a_fitting_iso_adds_nothing_to_the_script(self):
        assert drivers.guard_snippet('local:iso/virtio-win-0.1.189.iso') == ''

    def test_a_wrong_iso_stops_the_script_before_it_copies(self):
        snippet = drivers.guard_snippet('local:iso/virtio-win-0.1.262.iso')
        assert 'case "$VER_BUILD" in' in snippet
        assert '9600)' in snippet
        assert f'exit {drivers.REFUSED_EXIT_CODE}' in snippet

    def test_the_snippet_is_valid_shell(self):
        # The reason is prose with quotes and a URL in it, pasted into a here-doc-built
        # script. A snippet that does not parse takes the whole injection with it.
        import subprocess
        snippet = drivers.guard_snippet('local:iso/virtio-win.iso')
        check = subprocess.run(['bash', '-n'], input=f'VER_BUILD=9600\n{snippet}',
                               text=True, capture_output=True)
        assert check.returncode == 0, check.stderr

    def test_the_refused_build_is_the_only_one_that_exits(self):
        import subprocess
        snippet = drivers.guard_snippet('local:iso/virtio-win.iso')
        refused = subprocess.run(['bash', '-c', f'VER_BUILD=9600\n{snippet}\nexit 0'],
                                 capture_output=True, text=True)
        assert refused.returncode == drivers.REFUSED_EXIT_CODE
        assert 'REFUSED_DRIVER_RELEASE=' in refused.stdout

        allowed = subprocess.run(['bash', '-c', f'VER_BUILD=20348\n{snippet}\nexit 0'],
                                 capture_output=True, text=True)
        assert allowed.returncode == 0 and allowed.stdout == ''


class TestTheCatalogue:
    def test_every_required_release_can_be_fetched(self):
        for release in drivers.REQUIRED_RELEASE.values():
            assert release in drivers.CATALOGUE

    def test_every_entry_points_at_a_file_naming_its_release(self):
        for release, entry in drivers.CATALOGUE.items():
            assert drivers.release_of(entry['filename']) == release
            assert entry['url'].startswith('https://')
            assert entry['url'].endswith(entry['filename'])
