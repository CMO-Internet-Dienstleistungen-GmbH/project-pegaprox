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

    def test_a_current_guest_takes_a_current_release(self):
        assert drivers.refuse_iso(20348, 'local:iso/virtio-win-0.1.262.iso') is None
        assert drivers.refuse_iso(20348, 'local:iso/virtio-win.iso') is None

    def test_the_legacy_release_is_refused_for_everything_else(self):
        # 0.1.189 has no 2k22, w11 or 2k25 directory at all, so a modern guest given it
        # ends up with no storage driver registered — after the copy.
        reason = drivers.refuse_iso(20348, 'local:iso/virtio-win-0.1.189.iso')
        assert reason and 'only for Windows Server 2012 R2' in reason

    def test_a_guest_that_names_no_build_is_refused_whatever_the_iso(self):
        # The subdirectory is chosen from the build number, and without one the script
        # falls through to the newest variant. Handing Windows 11 drivers to a guest
        # nobody could identify is the failure this prevents, not a default.
        for iso in ('local:iso/virtio-win-0.1.189.iso', 'local:iso/virtio-win-0.1.302.iso'):
            assert drivers.refuse_iso(None, iso)


class TestTheGuardRunsOnTheNode:
    """The build number is only known on the node, inside the script that mounted the
    volume. Whether a release satisfies a build is known here, before anything runs."""

    def test_a_fitting_iso_still_guards_the_unreadable_case(self):
        # Nothing about the build is refused for this ISO, but a guest that reports no
        # build at all is refused whatever was chosen.
        snippet = drivers.guard_snippet('local:iso/virtio-win-0.1.189.iso')
        assert "''|*[!0-9]*)" in snippet
        assert f'-eq {drivers.LEGACY_BUILD}' not in snippet

    def test_the_legacy_iso_is_stopped_for_a_modern_guest(self):
        snippet = drivers.guard_snippet('local:iso/virtio-win-0.1.189.iso')
        assert f'-ne {drivers.LEGACY_BUILD}' in snippet

    def test_a_wrong_iso_stops_the_script_before_it_copies(self):
        snippet = drivers.guard_snippet('local:iso/virtio-win-0.1.262.iso')
        assert 'case "$VER_BUILD" in' in snippet
        assert f'-eq {drivers.LEGACY_BUILD}' in snippet
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


class TestTheIsoNameCannotRunCommands:
    """The refusal quotes the ISO's own name back at the reader, and that name is not
    ours: it is whatever a file on the node's storage is called. The script printing it
    runs as root on a Proxmox node, so a name containing `$(...)` inside a double-quoted
    shell string would be executed by whoever can upload an ISO."""

    def test_a_name_that_looks_like_a_command_stays_text(self, tmp_path):
        import subprocess
        marker = tmp_path / 'executed'
        evil = f'vm-pool:iso/virtio-win-$(touch {marker})-0.1.262.iso'

        snippet = drivers.guard_snippet(evil)
        result = subprocess.run(['bash', '-c', f'VER_BUILD=9600\n{snippet}\nexit 0'],
                                capture_output=True, text=True)

        assert result.returncode == drivers.REFUSED_EXIT_CODE
        assert not marker.exists(), 'the ISO name was executed by the shell'
        assert '$(touch' in result.stdout, 'the name should be reported verbatim'

    def test_a_name_full_of_quotes_does_not_break_the_script(self):
        import subprocess
        snippet = drivers.guard_snippet("vm-pool:iso/it's \"quoted\"; rm -rf /tmp/x.iso")
        result = subprocess.run(['bash', '-c', f'VER_BUILD=9600\n{snippet}\nexit 0'],
                                capture_output=True, text=True)
        assert result.returncode == drivers.REFUSED_EXIT_CODE
        assert 'rm -rf' in result.stdout


class TestWhichIsoTheWizardPreselects:
    """The wizard offers what the server would accept, so an operator does not pick a file
    the preflight then blocks."""

    ON_NODE = [
        {'volid': 'vm-pool:iso/gparted-live-1.7.0-8-amd64.iso', 'release': None},
        {'volid': 'vm-pool:iso/virtio-win-0.1.189.iso', 'release': '0.1.189'},
        {'volid': 'vm-pool:iso/virtio-win-0.1.262.iso', 'release': '0.1.262'},
    ]

    def test_server_2012_r2_gets_the_legacy_release(self):
        assert drivers.preferred_iso(9600, self.ON_NODE) == 'vm-pool:iso/virtio-win-0.1.189.iso'

    def test_a_current_guest_gets_the_newest_and_never_the_legacy_one(self):
        assert drivers.preferred_iso(20348, self.ON_NODE) == 'vm-pool:iso/virtio-win-0.1.262.iso'

    def test_a_current_guest_with_only_the_legacy_iso_gets_nothing(self):
        only_legacy = [self.ON_NODE[1]]
        assert drivers.preferred_iso(20348, only_legacy) is None

    def test_an_iso_whose_name_states_no_release_is_never_preselected(self):
        assert drivers.preferred_iso(20348, [self.ON_NODE[0]]) is None

    def test_what_to_fetch_when_nothing_fits(self):
        assert drivers.release_to_fetch(9600) == drivers.LEGACY_RELEASE
        assert drivers.release_to_fetch(20348) == drivers.newest_release(drivers.CATALOGUE)
        assert drivers.release_to_fetch(None) is None

    def test_releases_sort_by_number_not_by_text(self):
        # '0.1.9' must not outrank '0.1.302' the way string comparison would have it.
        assert drivers.newest_release(['0.1.9', '0.1.302']) == '0.1.302'


class TestAskingWhichReleaseIsCurrent:
    """The catalogue ages. A release hardcoded in March is still offered in November,
    long after the publisher has moved on — so the current one is asked for rather than
    remembered, and the catalogue is what answers when the question cannot be."""

    def setup_method(self):
        drivers._lookup.update({'release': None, 'checked_at': 0.0})

    def test_the_release_is_read_out_of_the_published_checksum_file(self, monkeypatch):
        # The file lists the stable build's RPMs; the version in those names is the only
        # machine-readable statement of "current" the project publishes.
        published = ('66f65c16ab3e8dfe12c2855a0d1ac303  virtio-win-0.1.999-1.noarch.rpm\n'
                     '09c1acd2ff72263c16a6afbf7a5f2e69  virtio-win-0.1.999-1.src.rpm\n')
        monkeypatch.setattr('urllib.request.urlopen',
                            lambda *a, **k: _FakeAnswer(published.encode()))

        assert drivers.refresh_current_release(force=True) == '0.1.999'
        assert '0.1.999' in drivers.offerable_releases()

    def test_a_publisher_that_cannot_be_reached_leaves_the_catalogue_alone(self, monkeypatch):
        def refuse(*args, **kwargs):
            raise OSError('no route to host')
        monkeypatch.setattr('urllib.request.urlopen', refuse)

        assert drivers.refresh_current_release(force=True) is None
        # And the wizard still has something to offer.
        assert set(drivers.offerable_releases()) == set(drivers.CATALOGUE)

    def test_the_pinned_legacy_release_survives_a_lookup(self, monkeypatch):
        monkeypatch.setattr('urllib.request.urlopen',
                            lambda *a, **k: _FakeAnswer(b'virtio-win-0.1.999-1.noarch.rpm'))
        drivers.refresh_current_release(force=True)

        # 0.1.189 is never "current" and must never fall out of the list: it is the only
        # release Server 2012 R2 may be given.
        assert drivers.LEGACY_RELEASE in drivers.offerable_releases()

    def test_a_discovered_release_can_be_downloaded(self):
        entry = drivers.catalogue_entry('0.1.999')
        assert entry['filename'] == 'virtio-win-0.1.999.iso'
        assert entry['url'].endswith('virtio-win-0.1.999-1/virtio-win-0.1.999.iso')

    def test_nonsense_is_not_turned_into_a_download(self):
        assert drivers.catalogue_entry('not-a-release') is None


class _FakeAnswer:
    """What urlopen returns, as far as this code uses it."""

    def __init__(self, payload):
        self._payload = payload

    def read(self, *args):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False
