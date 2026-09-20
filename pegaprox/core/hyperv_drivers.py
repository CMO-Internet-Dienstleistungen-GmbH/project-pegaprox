"""Which virtio-win release may be used for which Windows guest.

The driver ISO is not one choice per estate. Out-of-support Windows versions accept a
narrower set of signatures than current ones, and a driver whose chain the guest rejects
does not report an error: it is simply not loaded. A boot-start storage driver that is not
loaded stops the machine at `0xc0000428` naming `viostor.sys`; a network driver that is not
loaded leaves a guest that boots and cannot be reached. Both read as a failed conversion.

So the rule lives here, in one place, as data — with the guest build number on one side and
the release on the other — rather than as a sentence in a runbook that whoever is migrating
at three in the morning has not read.
"""

from __future__ import annotations

import logging
import re

#: The one build that may only be driven by the legacy release, and nothing else may be.
#: 9600 is Windows 8.1 and Windows Server 2012 R2 — the same build, and in virtio-win
#: 0.1.189 the same driver files byte for byte (checked across viostor, vioscsi, NetKVM,
#: Balloon and vioserial), so the two need not be told apart.
LEGACY_BUILD = 9600

#: What that build must be driven by. From virtio-win 0.1.221 the drivers are self-signed,
#: and a self-signed boot-start driver cannot load on x64 at all — the boot manager stops
#: at 0xc0000428 naming viostor.sys. In 0.1.189 the `2k12R2/amd64` driver carries
#: "Microsoft Code Verification Root" in its certificate table (measured 2026-09-16).
LEGACY_RELEASE = '0.1.189'

#: And the rule runs both ways: 0.1.189 is for Server 2012 R2 and for nothing else. It
#: also contains 2k16, 2k19 and w10 directories, and none of them are to be used — this
#: release is pinned to the one guest that has no alternative.
#:
#: It has no 2k22, w11 or 2k25 directory at all (measured on the ISO), so a modern guest
#: given this ISO ends up with no storage driver registered — after the copy.
LEGACY_ONLY_FOR = LEGACY_BUILD

#: Kept as the name callers and the API already read.
REQUIRED_RELEASE = {LEGACY_BUILD: LEGACY_RELEASE}

#: Releases this product can fetch onto a target node, and what each one is for.
#:
#: Every entry points into the archive directory of a specific release rather than at
#: `stable-virtio/virtio-win.iso`: that file carries no version in its name, so once it is
#: on a node nothing can say which release it is — which is exactly the question a 2012 R2
#: guest turns on.
#:
#: There is no publisher checksum to pin against. The `CHECKSUM` file next to the stable
#: build covers the RPMs, not the ISO, and the archive directories carry none at all
#: (checked 2026-09-16). The download is therefore verified by TLS to fedorapeople.org and
#: the hash of what arrived is recorded afterwards, so a second download can be compared
#: against the first.
CATALOGUE = {
    '0.1.189': {
        'url': ('https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/'
                'archive-virtio/virtio-win-0.1.189-1/virtio-win-0.1.189.iso'),
        'filename': 'virtio-win-0.1.189.iso',
        'note': 'Required for Windows Server 2012 R2 and Windows 8.1 (build 9600). '
                'Its drivers still chain to Microsoft Code Verification Root.',
    },
    '0.1.302': {
        'url': ('https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/'
                'archive-virtio/virtio-win-0.1.302-1/virtio-win-0.1.302.iso'),
        'filename': 'virtio-win-0.1.302.iso',
        'note': 'Current stable as of 2026-09-16. For guests that are still in support.',
    },
}

#: Where a required release can be obtained, so a refusal names the way out of itself.
RELEASE_SOURCE = {release: entry['url'] for release, entry in CATALOGUE.items()}

#: A virtio-win release inside a file name: `virtio-win-0.1.189.iso`, `virtio-win-0.1.262-2`.
logger = logging.getLogger(__name__)

_RELEASE_IN_NAME = re.compile(r'virtio[-_]win[-_](\d+\.\d+\.\d+)', re.IGNORECASE)


#: Exit code the injection script uses when it refuses to write drivers into a guest whose
#: build may not have them. Distinct from every code the surrounding script already uses,
#: so a refusal can be told apart from a failure.
REFUSED_EXIT_CODE = 9


def release_of(iso: str | None) -> str | None:
    """The virtio-win release an ISO's name declares, or None when it declares none.

    Read off the name rather than the contents on purpose: this has to answer before the
    ISO is mounted, and an operator who renames the file has removed the only statement
    about what is in it — which is itself worth refusing on, not worth guessing about.
    """
    match = _RELEASE_IN_NAME.search(iso or '')
    return match.group(1) if match else None


def required_release(build: int | str | None) -> str | None:
    """The release this guest build must be driven by, or None when it takes a current one.

    The build is the third component of the image's `Version` — `10.0.20348` is build
    20348. Major and minor say nothing: Windows 10, Windows 11 and every Server from 2016
    to 2025 all report `10.0`, and Server 2022 reports it while being neither Windows 10
    nor Server 2019.
    """
    try:
        return LEGACY_RELEASE if int(build) == LEGACY_BUILD else None
    except (TypeError, ValueError):
        return None


def refuses_build(release: str | None, build: int | str | None) -> bool:
    """Is this release the wrong one for this guest, in either direction?"""
    try:
        number = int(build)
    except (TypeError, ValueError):
        return False
    if number == LEGACY_BUILD:
        return release != LEGACY_RELEASE
    # The legacy release is pinned to the one guest that needs it. Its other directories
    # exist, are old, and are not what a supported guest is given.
    return release == LEGACY_RELEASE


def unreadable_build(build: int | str | None) -> bool:
    """Did the guest fail to tell us which Windows this is?

    It matters more than it looks. The driver subdirectory is chosen from this number, and
    when it cannot be read the choice falls through to the newest variant — so a guest
    whose registry could not be opened is handed Windows 11 drivers. Where the answer
    decides whether a driver can load at all, not knowing is a reason to stop, not a
    reason to pick the most likely one.
    """
    try:
        return int(build) <= 0
    except (TypeError, ValueError):
        return True


def refuse_iso(build: int | str | None, iso: str | None) -> str | None:
    """Why these drivers must not be injected into this guest, or None to proceed.

    Refuses an ISO whose release cannot be read as well as one that is plainly wrong. An
    unnamed release is not evidence of the right one, and injecting on a maybe produces
    exactly the silent failure this exists to prevent.
    """
    if unreadable_build(build):
        return ('This guest does not report a Windows build number, so which driver '
                'release it may be given cannot be decided. Without it the injection '
                'would fall through to the newest variant, which an out-of-support '
                'Windows cannot load. Install libhivex-bin on the target node — hivexsh '
                'is what reads the number — or import without driver injection and '
                'install them from inside the guest.')

    found = release_of(iso)
    if not refuses_build(found, build):
        return None

    if int(build) == LEGACY_BUILD:
        source = RELEASE_SOURCE.get(LEGACY_RELEASE)
        where = f' It can be downloaded from {source}' if source else ''
        if found is None:
            return (f'Windows Server 2012 R2 (build {LEGACY_BUILD}) may only be given '
                    f'virtio-win {LEGACY_RELEASE} drivers, and {iso!r} does not say which '
                    f'release it is. Choose a file whose name carries the release.{where}')
        return (f'Windows Server 2012 R2 (build {LEGACY_BUILD}) may only be given '
                f'virtio-win {LEGACY_RELEASE} drivers; {iso!r} is {found}. Later releases '
                f'are signed in a way this Windows version does not accept, so the driver '
                f'is silently not loaded and the VM stops at 0xc0000428.{where}')

    return (f'virtio-win {LEGACY_RELEASE} is only for Windows Server 2012 R2, and this '
            f'guest is build {build}. That release has no driver directory for it at all '
            f'— the injection would register no storage driver, and the VM would have no '
            f'way to reach its disk. Choose a current release instead.')


def guard_snippet(iso: str | None) -> str:
    """Shell that stops the injection when this ISO may not drive the guest it found.

    The guest's build number is only known on the node, inside the script that mounted the
    volume — but whether a release may drive a given build is known here, before anything
    runs. Python works out what this ISO is good for; the script compares one number.

    Three refusals, and the first holds whatever ISO was chosen: a guest that does not
    report a build number has not said which Windows it is, and the surrounding script
    answers that by falling through to the newest driver variant. Handing Windows 11
    drivers to a guest nobody could identify is the failure this prevents, not a default.
    """
    unreadable = refuse_iso(None, iso)
    legacy = refuse_iso(LEGACY_BUILD, iso)
    # Any build that is not the legacy one behaves the same way, so one stands for all.
    modern = refuse_iso(LEGACY_BUILD + 1, iso)

    lines = ['# Fork issue #15 — which driver release this guest may be given.\n',
             'case "$VER_BUILD" in\n',
             f"  ''|*[!0-9]*) echo 'REFUSED_DRIVER_RELEASE={_sq(unreadable)}'; "
             f'exit {REFUSED_EXIT_CODE} ;;\n',
             'esac\n']
    if legacy:
        lines.append(f'if [ "$VER_BUILD" -eq {LEGACY_BUILD} ]; then\n'
                     f"  echo 'REFUSED_DRIVER_RELEASE={_sq(legacy)}'; "
                     f'exit {REFUSED_EXIT_CODE}\n'
                     'fi\n')
    if modern:
        lines.append(f'if [ "$VER_BUILD" -ne {LEGACY_BUILD} ]; then\n'
                     f"  echo 'REFUSED_DRIVER_RELEASE={_sq(modern)}'; "
                     f'exit {REFUSED_EXIT_CODE}\n'
                     'fi\n')
    return ''.join(lines)


def _sq(text: str) -> str:
    """The body of a single-quoted shell string: end, escaped quote, reopen."""
    return text.replace("'", "'\\''")


def release_key(release: str | None) -> tuple:
    """A release as something that can be ordered. Unreadable sorts lowest."""
    try:
        return tuple(int(part) for part in (release or '').split('.'))
    except (TypeError, ValueError):
        return ()


def newest_release(releases) -> str | None:
    """The highest release in a list, ignoring the legacy one and anything unnamed."""
    usable = [r for r in releases if r and r != LEGACY_RELEASE and release_key(r)]
    return max(usable, key=release_key) if usable else None


def preferred_iso(build: int | str | None, available: list[dict] | None) -> str | None:
    """Which of the ISOs already on the node this guest should be given, if any.

    Server 2012 R2 takes 0.1.189 and nothing else. Every other guest takes the newest
    release present — and never 0.1.189, which has no directory for it.
    """
    by_release = {}
    for entry in available or []:
        release = entry.get('release') or release_of(entry.get('volid'))
        if release:
            by_release.setdefault(release, entry.get('volid'))

    try:
        number = int(build)
    except (TypeError, ValueError):
        return None

    if number == LEGACY_BUILD:
        return by_release.get(LEGACY_RELEASE)
    newest = newest_release(by_release)
    return by_release.get(newest) if newest else None


def release_to_fetch(build: int | str | None) -> str | None:
    """Which release the node should download when it has nothing suitable."""
    try:
        number = int(build)
    except (TypeError, ValueError):
        return None
    if number == LEGACY_BUILD:
        return LEGACY_RELEASE
    return newest_release(CATALOGUE)


# ---------------------------------------------------------------------------
# What the current stable release is, asked rather than remembered
# ---------------------------------------------------------------------------

#: Where the publisher states its current build. The file lists the RPMs of the stable
#: release — `virtio-win-0.1.302-1.noarch.rpm` — and the version in those names is the
#: only machine-readable statement of "current" the project publishes. There is no
#: checksum for the ISO itself, here or in the archive directories (checked 2026-09-16).
STABLE_CHECKSUM_URL = ('https://fedorapeople.org/groups/virt/virtio-win/'
                       'direct-downloads/stable-virtio/CHECKSUM')

#: Where a release's ISO lives once its number is known.
_ARCHIVE_URL = ('https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/'
                'archive-virtio/virtio-win-{release}-1/virtio-win-{release}.iso')

#: How long a looked-up release stays good. The publisher ships a few times a year, so
#: asking once a day is generous and asking on every wizard open would be rude.
LOOKUP_MAX_AGE = 24 * 60 * 60

#: Seconds to wait for the publisher. Short on purpose: this runs while somebody waits
#: for a page, and the catalogue below is a perfectly good answer.
LOOKUP_TIMEOUT = 10

_lookup = {'release': None, 'checked_at': 0.0}


def catalogue_entry(release: str) -> dict | None:
    """One release as something that can be downloaded, known or newly discovered."""
    if release in CATALOGUE:
        return CATALOGUE[release]
    if not release_key(release):
        return None
    return {
        'url': _ARCHIVE_URL.format(release=release),
        'filename': f'virtio-win-{release}.iso',
        'note': 'Current stable, as published by the virtio-win project.',
    }


def refresh_current_release(force: bool = False) -> str | None:
    """Ask the publisher which release is current. Returns it, or None on any failure.

    Cached for a day. Never raises and never blocks anything: a lookup that fails leaves
    the built-in catalogue in place, which is a working answer rather than an error — it
    is merely one that ages.
    """
    import time
    import urllib.request

    now = time.time()
    if not force and _lookup['release'] and (now - _lookup['checked_at']) < LOOKUP_MAX_AGE:
        return _lookup['release']

    try:
        with urllib.request.urlopen(STABLE_CHECKSUM_URL, timeout=LOOKUP_TIMEOUT) as answer:
            text = answer.read(64_000).decode('utf-8', 'replace')
    except Exception:
        logger.info('Could not ask which virtio-win release is current; '
                    'using the built-in catalogue', exc_info=True)
        _lookup['checked_at'] = now
        return _lookup['release']

    found = {match.group(1) for match in _RELEASE_IN_NAME.finditer(text)}
    newest = newest_release(found)
    if newest:
        _lookup.update({'release': newest, 'checked_at': now})
        logger.info('Current virtio-win release is %s', newest)
    else:
        _lookup['checked_at'] = now
    return _lookup['release']


def offerable_releases() -> dict:
    """Every release the wizard may offer for download.

    The built-in catalogue plus whatever the publisher currently calls stable. The
    catalogue is not replaced by the lookup: 0.1.189 is pinned for Server 2012 R2 and is
    never "current", and a lookup that fails must not empty the list.
    """
    entries = dict(CATALOGUE)
    current = _lookup['release']
    if current and current not in entries:
        entry = catalogue_entry(current)
        if entry:
            entries[current] = entry
    return entries


def refresh_in_background() -> None:
    """Start the lookup without making anybody wait for it.

    The wizard answers from what is known and is right one page-open later. Blocking a
    page for ten seconds to learn a version number that changes a few times a year is the
    wrong trade, and a publisher that is slow or unreachable must not be able to make the
    migration wizard slow or unreachable with it.
    """
    import threading
    import time

    if _lookup['release'] and (time.time() - _lookup['checked_at']) < LOOKUP_MAX_AGE:
        return
    threading.Thread(target=refresh_current_release, daemon=True,
                     name='virtio-win-release-lookup').start()
