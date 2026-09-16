# Fork issue #15 — a Hyper-V host refuses a connection for reasons that read alike in a log
# and are entirely different jobs to fix. pegaprox/core/hyperv_errors.py reduces each one to
# a kind that names where to go and look, and these hold that the kinds stay distinct.
#
# Ordering between the marker groups is the part that breaks silently: WinRM carries
# authorization faults over HTTP 401, so a single message can match both groups and the
# wrong one wins by accident. Network-free, no Hyper-V host required.

import pytest

from pegaprox.core import hyperv_errors as hv


@pytest.mark.parametrize('exc_type, message, expected', [
    ('SSLError', 'certificate verify failed: unable to get local issuer certificate', hv.KIND_CERTIFICATE),
    ('MaxRetryError', 'certificate verify failed', hv.KIND_CERTIFICATE),
    ('ConnectionError', 'Connection refused', hv.KIND_UNREACHABLE),
    ('ConnectTimeout', 'timed out', hv.KIND_UNREACHABLE),
    ('ConnectionError', 'Name or service not known', hv.KIND_UNREACHABLE),
    ('ReadTimeout', 'Read timed out', hv.KIND_TIMEOUT),
    ('AuthenticationError', 'Failed to authenticate the user', hv.KIND_AUTHENTICATION),
    ('WSManAuthenticationError', 'the server returned 401 Unauthorized', hv.KIND_AUTHENTICATION),
    ('RuntimeError', 'Logon failure: unknown user name or bad password', hv.KIND_AUTHENTICATION),
    ('WSManFaultError', 'Access is denied.', hv.KIND_AUTHORIZATION),
    ('RuntimeError', 'The client is not authorized to perform this operation', hv.KIND_AUTHORIZATION),
    ('RuntimeError', "The term 'Get-VM' is not recognized as the name of a cmdlet", hv.KIND_MISSING_FEATURE),
    ('ModuleNotFoundError', "No module named 'pypsrp'", hv.KIND_CLIENT_DEPENDENCY),
])
def test_each_cause_gets_its_own_kind(exc_type, message, expected):
    assert hv.classify(exc_type, message) == expected


def test_missing_rights_wins_over_the_http_status_that_carries_them():
    # WinRM reports authorization faults over HTTP 401, so the message holds both markers.
    # The rights marker is the specific one; the other reading sends somebody to reset a
    # password that was never wrong.
    both = 'The WS-Management service cannot complete the operation. Access is denied. (401 Unauthorized)'
    assert hv.classify('WSManFaultError', both) == hv.KIND_AUTHORIZATION


def test_a_plain_401_without_a_rights_marker_is_still_authentication():
    assert hv.classify('RuntimeError', 'the server returned 401 Unauthorized') == hv.KIND_AUTHENTICATION


def test_a_read_timeout_is_not_an_unreachable_host():
    # ReadTimeout means the endpoint accepted the connection and went quiet. Calling it
    # unreachable sends somebody to the firewall for a host-side timeout.
    assert hv.classify('ReadTimeout', 'Read timed out') == hv.KIND_TIMEOUT
    assert hv.classify('ConnectTimeout', 'timed out') == hv.KIND_UNREACHABLE


def test_a_missing_client_library_is_not_a_host_problem():
    kind = hv.classify('ModuleNotFoundError', "No module named 'pypsrp'")
    assert kind == hv.KIND_CLIENT_DEPENDENCY
    assert kind != hv.KIND_MISSING_FEATURE


def test_an_unmatched_failure_stays_unknown_instead_of_guessing():
    assert hv.classify('RuntimeError', 'something entirely new happened') == hv.KIND_UNKNOWN


def test_the_five_connection_causes_are_five_distinct_kinds():
    kinds = {
        hv.classify('ConnectionError', 'Connection refused'),
        hv.classify('SSLError', 'certificate verify failed'),
        hv.classify('AuthenticationError', 'Logon failure'),
        hv.classify('WSManFaultError', 'Access is denied.'),
        hv.classify('RuntimeError', "The term 'Get-VM' is not recognized"),
    }
    assert len(kinds) == 5


@pytest.mark.parametrize('kind', [
    hv.KIND_UNREACHABLE, hv.KIND_TIMEOUT, hv.KIND_CERTIFICATE, hv.KIND_AUTHENTICATION,
    hv.KIND_AUTHORIZATION, hv.KIND_MISSING_FEATURE, hv.KIND_CLIENT_DEPENDENCY, hv.KIND_UNKNOWN,
])
def test_every_failure_kind_names_a_next_step(kind):
    assert hv.remedy(kind)


def test_success_has_no_remedy():
    assert hv.remedy(hv.KIND_OK) == ''


def test_an_unknown_kind_still_returns_guidance_rather_than_raising():
    assert hv.remedy('not-a-kind-at-all') == hv.REMEDIES[hv.KIND_UNKNOWN]


class TestHyperVError:
    def test_it_classifies_an_arbitrary_transport_exception(self):
        err = hv.HyperVError.from_exception(ConnectionError('Connection refused'), 'connecting')
        assert err.kind == hv.KIND_UNREACHABLE
        assert 'connecting' in err.message
        assert 'Connection refused' in err.message
        assert err.remedy

    def test_its_dict_carries_the_next_step_and_nothing_else(self):
        err = hv.HyperVError('nope', kind=hv.KIND_AUTHORIZATION)
        assert set(err.to_dict()) == {'kind', 'message', 'remedy'}
        assert err.to_dict()['kind'] == hv.KIND_AUTHORIZATION

    def test_it_is_raisable_and_catchable_as_an_exception(self):
        with pytest.raises(hv.HyperVError) as caught:
            raise hv.HyperVError('boom', kind=hv.KIND_TIMEOUT)
        assert caught.value.kind == hv.KIND_TIMEOUT
