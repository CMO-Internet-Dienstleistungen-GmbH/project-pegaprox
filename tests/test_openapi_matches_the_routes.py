"""The published API description has to match the API.

docs/openapi.json is what external tooling reads (#104 Terraform, #693 Ansible).
A spec that drifts from the code is worse than no spec, because it looks
authoritative. So this rebuilds the description from the live route table and
compares it against the committed file: a new route, a removed one, or a changed
permission turns this red until someone re-runs

    venv/bin/python -m pegaprox.cli.gen_openapi -o docs/openapi.json

Body schemas are deliberately NOT compared - they are hand-written per resource
group and the generator does not produce them, so comparing them would fight the
people filling them in.

MK Sep 2026
"""
import json
import os

import pytest

from pegaprox.cli.gen_openapi import build

SPEC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    'docs', 'openapi.json')


def _ops(paths):
    """{(path, method): (sorted perms, sorted roles, auth kind)}"""
    out = {}
    for path, methods in paths.items():
        for method, op in methods.items():
            out[(path, method)] = (
                tuple(sorted(op.get('x-pegaprox-permissions', []))),
                tuple(sorted(op.get('x-pegaprox-roles', []))),
                op.get('x-pegaprox-auth'),
            )
    return out


@pytest.fixture(scope='module')
def committed():
    with open(SPEC, encoding='utf-8') as fh:
        return json.load(fh)


def test_the_spec_is_structurally_sound(committed):
    assert committed['openapi'].startswith('3.1')
    ids = [o['operationId'] for m in committed['paths'].values() for o in m.values()]
    assert len(ids) == len(set(ids)), 'operationId must be unique across the document'
    for path in committed['paths']:
        assert path.startswith('/'), path
        assert '<' not in path, f'werkzeug syntax leaked into the spec: {path}'


def test_every_live_route_is_described(api, committed):
    live, doc = _ops(build(api.app)), _ops(committed['paths'])

    missing = sorted(live.keys() - doc.keys())
    stale = sorted(doc.keys() - live.keys())
    assert not missing, (
        f'{len(missing)} route(s) exist but are not in docs/openapi.json, '
        f'e.g. {missing[:5]} - re-run python -m pegaprox.cli.gen_openapi -o docs/openapi.json')
    assert not stale, (
        f'{len(stale)} route(s) in docs/openapi.json no longer exist, '
        f'e.g. {stale[:5]} - re-run the generator')


def test_the_documented_permissions_are_the_enforced_ones(api, committed):
    """The interesting half. If a route's permission changes and the spec keeps
    the old one, we are publishing a false claim about who can call it."""
    live, doc = _ops(build(api.app)), _ops(committed['paths'])
    drift = {k: (doc[k], live[k]) for k in live.keys() & doc.keys() if doc[k] != live[k]}
    assert not drift, (
        f'{len(drift)} route(s) document different auth than they enforce: '
        + '; '.join(f'{m.upper()} {p}: spec={d} code={l}'
                    for (p, m), (d, l) in list(drift.items())[:4]))
