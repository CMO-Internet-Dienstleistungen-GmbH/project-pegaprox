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

import re

#: Windows build numbers that may only be driven by a specific virtio-win release, and the
#: release that is required. Build 9600 is Windows Server 2012 R2 (and Windows 8.1).
#:
#: 2012 R2: from virtio-win 0.1.221 on, the drivers are self-signed, and a self-signed
#: boot-start driver cannot load on x64 at all. 0.1.208 is the last release carrying a
#: cross-certificate; 0.1.189 is the one whose `2k12R2/amd64` drivers chain to Microsoft
#: Code Verification Root, and it is what CMO runs with.
REQUIRED_RELEASE = {
    9600: '0.1.189',
}

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
_RELEASE_IN_NAME = re.compile(r'virtio[-_]win[-_](\d+\.\d+\.\d+)', re.IGNORECASE)


def release_of(iso: str | None) -> str | None:
    """The virtio-win release an ISO's name declares, or None when it declares none.

    Read off the name rather than the contents on purpose: this has to answer before the
    ISO is mounted, and an operator who renames the file has removed the only statement
    about what is in it — which is itself worth refusing on, not worth guessing about.
    """
    match = _RELEASE_IN_NAME.search(iso or '')
    return match.group(1) if match else None


def required_release(build: int | str | None) -> str | None:
    """The release this guest build must be driven by, or None when any will do."""
    try:
        return REQUIRED_RELEASE.get(int(build))
    except (TypeError, ValueError):
        return None


def refuse_iso(build: int | str | None, iso: str | None) -> str | None:
    """Why these drivers must not be injected into this guest, or None to proceed.

    Refuses an ISO whose release cannot be read as well as one that is plainly wrong. An
    unnamed release is not evidence of the right one, and injecting on a maybe produces
    exactly the silent failure this exists to prevent.
    """
    required = required_release(build)
    if not required:
        return None

    found = release_of(iso)
    if found == required:
        return None

    source = RELEASE_SOURCE.get(required)
    where = f' It can be downloaded from {source}' if source else ''
    if found is None:
        return (f'This guest (build {build}) may only be given virtio-win {required} '
                f'drivers, and {iso!r} does not say which release it is. Put a file whose '
                f'name carries the release on the node and choose it in the wizard.{where}')
    return (f'This guest (build {build}) may only be given virtio-win {required} drivers; '
            f'{iso!r} is {found}. Later releases are signed in a way this Windows version '
            f'does not accept, so the driver is silently not loaded and the VM stops at '
            f'0xc0000428.{where}')


#: Exit code the injection script uses when it refuses to write drivers into a guest whose
#: build may not have them. Distinct from every code the surrounding script already uses,
#: so a refusal can be told apart from a failure.
REFUSED_EXIT_CODE = 9


def guard_snippet(iso: str | None) -> str:
    """Shell that stops the injection when this ISO may not drive the guest it found.

    The guest's build number is only known on the node, inside the script that mounted the
    volume — but whether a given release satisfies a given build is known here, before
    anything runs. So each build this ISO would be wrong for is rendered as a case branch
    that exits; the builds it is right for produce no branch at all, and the script
    proceeds exactly as it did.

    Returns an empty string when nothing is refused, which is the ordinary case.
    """
    branches = []
    for build in sorted(REQUIRED_RELEASE):
        reason = refuse_iso(build, iso)
        if not reason:
            continue
        escaped = reason.replace('\\', '\\\\').replace('"', '\\"')
        branches.append(f'  {build}) echo "REFUSED_DRIVER_RELEASE={escaped}"; '
                        f'exit {REFUSED_EXIT_CODE} ;;\n')
    if not branches:
        return ''
    return ('case "$VER_BUILD" in\n' + ''.join(branches) + 'esac\n')
