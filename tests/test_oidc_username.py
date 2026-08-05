# Username derivation from OIDC claims — oidc_derive_username().
#
# The username is the users-table primary key, so how it is derived from the
# IdP's claims decides which account an OIDC login lands on. This used to
# truncate at '@', which collapsed bob@corp.com and bob@partner.com onto a
# single 'bob' account — the second one to log in inherited the first one's
# role and tenant. The full name is now kept, with a back-compat lookup so
# installs that already provisioned the truncated form aren't orphaned.
#
# Harness note: the `db` fixture is required for every case, not just the
# legacy one — oidc_derive_username() calls load_users() whenever the derived
# name contains '@', and without the fixture that would hit the real CONFIG_DIR.

import pytest

from tests.conftest import _seed_user

from pegaprox.utils.oidc import oidc_derive_username


def test_preferred_username_keeps_the_domain(db):
    assert oidc_derive_username(
        {'preferred_username': 'bob@example.com', 'sub': 's1'}
    ) == 'bob@example.com'


def test_two_domains_stay_distinct(db):
    """The actual bug: these must not collapse onto one account."""
    corp = oidc_derive_username({'preferred_username': 'bob@corp.com'})
    partner = oidc_derive_username({'preferred_username': 'bob@partner.com'})
    assert corp != partner


def test_plain_preferred_username_is_unchanged(db):
    assert oidc_derive_username(
        {'preferred_username': 'John.Doe', 'sub': 's2'}
    ) == 'john.doe'


def test_falls_back_to_email(db):
    assert oidc_derive_username(
        {'email': 'carol@example.com', 'sub': 's3'}
    ) == 'carol@example.com'


def test_falls_back_to_sub_when_claims_are_empty(db):
    assert oidc_derive_username({'sub': 'abcdef0123456789'}) == 'oidc_abcdef012345'


def test_existing_truncated_account_keeps_its_key(db):
    """Pre-change installs stored 'bob'; that login must not fork a new account."""
    _seed_user(db, 'bob')
    assert oidc_derive_username({'preferred_username': 'bob@example.com'}) == 'bob'


def test_unrelated_existing_account_does_not_capture_the_login(db):
    _seed_user(db, 'alice')
    assert oidc_derive_username(
        {'preferred_username': 'bob@example.com'}
    ) == 'bob@example.com'
