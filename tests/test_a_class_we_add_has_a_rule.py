"""Every CSS class this fork adds has to exist in the stylesheet that ships.

PegaProx serves a **static, pre-built** Tailwind file — `static/css/tailwind.min.css`,
switched to in upstream #118 because the CDN's JIT compiler broke on air-gapped
installations. There is no build step that scans the source for class names. A class
nobody generated a rule for is therefore not a style that is subtly off: it is no style at
all, and the markup renders as though the attribute were absent.

This was not theoretical. Two fixes shipped in a release and did nothing:
`max-h-[26rem]` was supposed to stop the migration list pushing its own error log off the
screen, and `break-words` was supposed to make a long error message in a toast wrap. Both
were reported as fixed, both were invisible in the CSS, and neither had any effect on the
running product.

Upstream's own source uses a few hundred undefined classes, which is its problem and not
one this test tries to solve. What it does hold is the line: a class the fork introduces
must have a rule. The baseline is read from the release tag at run time, so the test needs
no list to maintain and measures exactly what we added.
"""

import re
import subprocess
from pathlib import Path

import pytest

WEB_SRC = Path('web/src')
STYLESHEETS = (Path('static/css/tailwind.min.css'),)

#: Classes that are ours by name and are styled by the application's own CSS rather than
#: by Tailwind — the corporate layout defines them in the HTML shell.
OUR_OWN = {'corp-action-btn', 'danger'}

#: The upstream release this fork is built on. Its class usage is the baseline: anything
#: undefined that already appears there is not something this patch set introduced.
BASE_REF = 'v1.1.1'

_CLASS_ATTR = re.compile(r'className=(?:"([^"]*)"|\{`([^`]*)`\})')
_TEMPLATE_HOLE = re.compile(r'\$\{[^}]*\}')
_PLAIN_CLASS = re.compile(r'^[a-zA-Z][\w:./\[\]%#-]*$')


def _classes(text):
    found = set()
    for match in _CLASS_ATTR.finditer(text):
        blob = _TEMPLATE_HOLE.sub(' ', match.group(1) or match.group(2) or '')
        found.update(token for token in blob.split() if _PLAIN_CLASS.match(token))
    return found


def _defined():
    defined = set()
    for sheet in STYLESHEETS:
        if not sheet.exists():
            pytest.skip(f'{sheet} is not in this checkout')
        css = sheet.read_text()
        for match in re.finditer(r'\.((?:\\.|[A-Za-z0-9_-])+)(?=[\s,:{>~+\[])', css):
            defined.add(re.sub(r'\\(.)', r'\1', match.group(1)))
    return defined


def _baseline(path):
    """What the upstream release's version of this file used, or nothing when it is ours."""
    result = subprocess.run(['git', 'show', f'{BASE_REF}:{path}'],
                            capture_output=True, text=True)
    return _classes(result.stdout) if result.returncode == 0 else set()


@pytest.mark.parametrize('path', sorted(WEB_SRC.glob('*.js')), ids=lambda p: p.name)
def test_the_classes_this_fork_adds_are_in_the_stylesheet(path):
    added = _classes(path.read_text()) - _baseline(path) - OUR_OWN
    missing = sorted(added - _defined())
    assert not missing, (
        f'{path} uses {len(missing)} class(es) the shipped stylesheet does not define, so '
        f'they have no effect at all: {", ".join(missing)}')
