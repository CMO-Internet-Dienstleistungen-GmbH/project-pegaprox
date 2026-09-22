"""CWE-117 belongs at the sink, not at seventy-five call sites.

Fourteen findings named fourteen files for the same thing: a group name, a VM name, a
URL, an error string - values the caller chose - end up in a log line. CR and LF forge
a line. ESC does more: `\\x1b[2K\\r` erases what is there and repaints it, so the
operator tailing the log sees whatever the attacker wanted them to see, and the
sanitiser that existed stripped CR/LF and left ESC alone.

Wrapping the call sites would have been seventy-five edits and a seventy-sixth somebody
forgets next month. The filter sits on the root handlers instead and covers the lines
written after today too.

Tracebacks are deliberately exempt. They arrive through exc_info and the formatter
appends them after the message, so they keep their newlines - they are ours, not the
caller's, and an unreadable traceback helps nobody.

Aikido ai_pentest: the log-injection half of the medium/low sweep. MK
"""
import io
import logging

import pytest

from pegaprox.utils.sanitization import (LogInjectionFilter, install_log_injection_filter,
                                         sanitize_log_message)


# --- the sanitiser itself ------------------------------------------------------------

def test_cr_and_lf_still_go():
    assert sanitize_log_message('a\r\nb') == 'a  b'


def test_escape_goes_now():
    """The one the old version missed: this is a repaint, not an extra line."""
    out = sanitize_log_message('name\x1b[2K\rAudit: admin - deleted everything')

    assert '\x1b' not in out and '\r' not in out
    assert 'Audit' in out, 'the text is kept, only its power is removed'


@pytest.mark.parametrize('ch', ['\x00', '\x07', '\x08', '\x0b', '\x0c', '\x1b', '\x7f', '\x85'])
def test_the_whole_control_range_goes(ch):
    assert ch not in sanitize_log_message(f'a{ch}b')


def test_tab_stays():
    """Legitimate in action strings, and it cannot move a cursor about."""
    assert sanitize_log_message('a\tb') == 'a\tb'


def test_unicode_line_separators_still_go():
    assert sanitize_log_message('a b c') == 'a b c'


def test_ordinary_text_is_untouched():
    assert sanitize_log_message('cluster-1 node pve01 ok') == 'cluster-1 node pve01 ok'


def test_none_becomes_empty():
    assert sanitize_log_message(None) == ''


# --- the filter ----------------------------------------------------------------------

@pytest.fixture
def captured():
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter('%(message)s'))
    log = logging.getLogger('pegaprox.test.loginj')
    log.handlers[:] = [handler]
    log.setLevel(logging.DEBUG)
    log.propagate = False
    install_log_injection_filter(log)
    return log, buf


def test_an_fstring_message_is_neutralised(captured):
    log, buf = captured
    name = 'gruppe\nAudit: admin - deleted everything'

    log.info(f'[XCLB] balancing group {name}')

    assert buf.getvalue().count('\n') == 1, buf.getvalue()


def test_percent_style_arguments_are_neutralised_too(captured):
    """Half the tree logs with %s, and the value lands in record.args, not record.msg."""
    log, buf = captured

    log.warning('user %s did %s', 'bob\nAudit: forged', 'a\rb')

    assert buf.getvalue().count('\n') == 1
    assert 'Audit: forged' in buf.getvalue(), 'the text stays, on one line'


def test_a_traceback_keeps_its_newlines(captured):
    """exc_info is appended by the formatter after the message - ours, not the caller's."""
    log, buf = captured
    try:
        1 / 0
    except ZeroDivisionError:
        log.error('real failure', exc_info=True)

    out = buf.getvalue()
    assert 'Traceback' in out and 'ZeroDivisionError' in out
    assert out.count('\n') > 2


def test_escape_sequences_do_not_survive_into_the_stream(captured):
    log, buf = captured

    log.info('%s', 'x\x1b[31mred\x1b[0m')

    assert '\x1b' not in buf.getvalue()


def test_installing_twice_does_not_stack_filters(captured):
    """Per HANDLER, not per logger - and the count is per handler on purpose: pytest's
    own logging plugin attaches capture handlers to this logger after the fixture runs,
    and each of them needs the filter too, or a captured line is an unfiltered line."""
    log, _ = captured
    install_log_injection_filter(log)
    install_log_injection_filter(log)

    per_handler = [sum(1 for f in h.filters if isinstance(f, LogInjectionFilter))
                   for h in log.handlers]
    assert per_handler and max(per_handler) == 1, per_handler


def test_every_handler_on_the_logger_gets_one(captured):
    """A handler added after us is a hole: install covers whatever is attached now."""
    log, _ = captured
    extra = logging.StreamHandler(io.StringIO())
    log.addHandler(extra)
    install_log_injection_filter(log)

    assert any(isinstance(f, LogInjectionFilter) for f in extra.filters)


def test_a_non_string_message_is_left_alone(captured):
    """logging accepts any object as msg; do not stringify it early."""
    log, buf = captured

    log.info({'a': 1})

    assert "{'a': 1}" in buf.getvalue()


# --- the wiring ----------------------------------------------------------------------

def test_the_app_installs_it():
    """Both neutralisers are wired up where logging is configured.

    The window is deliberately generous - it exists to catch the call drifting off to
    some unrelated part of app.py, not to police comment length. The record sanitiser
    matters more than the handler filter: the filter only reaches handlers that already
    exist, and the per-cluster loggers in core/manager.py and core/xcpng.py attach theirs
    much later. See test_a_logger_created_after_startup_is_still_sanitised.
    """
    import inspect
    import pegaprox.app as app

    src = inspect.getsource(app)
    i = src.index('logging.basicConfig(')
    window = src[i:i + 2500]
    assert 'install_log_record_sanitizer()' in window, \
        'the record sanitiser is not installed with the logging setup'
    assert 'install_log_injection_filter()' in window, \
        'the handler filter is not installed with the logging setup'


# --- follow-up: the sink we missed -----------------------------------------------
# The handler filter was written to sit "at the sink instead of at seventy-five call
# sites". It sits on HANDLERS, and install_log_injection_filter() only walks the ones
# that exist when it runs. core/manager.py and core/xcpng.py give each cluster its own
# logger with its own file+console handler, attached when that cluster is constructed -
# after startup. A cluster logger emits through its own handlers BEFORE propagating to
# root, so the per-cluster log file kept the raw line while the main log got the clean
# one. install_log_record_sanitizer() moves the work to record construction. MK

import io
import logging


def _sanitizer():
    """The record sanitiser if this build has one, otherwise a no-op.

    Deliberately not a bare import: against a build without it, these tests must fail on
    what reaches the handler, not on a missing name. An ImportError would be red for the
    wrong reason and would prove nothing about the defect.
    """
    try:
        from pegaprox.utils.sanitization import install_log_record_sanitizer
        return install_log_record_sanitizer
    except ImportError:
        return lambda: None


def _isolated_root():
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(logging.Formatter('%(message)s'))
    root = logging.getLogger()
    old = root.handlers[:]
    root.handlers = [h]
    return buf, root, old


def test_a_logger_created_after_startup_is_still_sanitised():
    """The property: a handler attached later must not receive a forgeable record.

    Drives the real shape - install first, create the cluster logger afterwards, and
    read what THAT handler wrote, not what root wrote.
    """
    from pegaprox.utils.sanitization import install_log_injection_filter
    _root_buf, root, old = _isolated_root()
    try:
        _sanitizer()()
        install_log_injection_filter()

        later = io.StringIO()
        lh = logging.StreamHandler(later)
        lh.setFormatter(logging.Formatter('%(message)s'))
        clog = logging.getLogger('PegaProx_test_cluster_after_startup')
        clog.handlers = [lh]
        clog.setLevel(logging.INFO)

        clog.warning("vm rename to %s",
                     "harmless\r\n2026-01-01 00:00:00 ADMIN deleted all backups")
        written = later.getvalue()

        assert '\r' not in written and written.count('\n') == 1, (
            f'a forged line reached the per-cluster handler: {written!r}')
        assert 'ADMIN deleted all backups' in written, \
            'the value was dropped entirely instead of neutralised'
    finally:
        root.handlers = old


def test_escape_sequences_do_not_reach_a_late_handler():
    _root_buf, root, old = _isolated_root()
    try:
        _sanitizer()()
        later = io.StringIO()
        lh = logging.StreamHandler(later)
        lh.setFormatter(logging.Formatter('%(message)s'))
        clog = logging.getLogger('PegaProx_test_cluster_esc')
        clog.handlers = [lh]
        clog.setLevel(logging.INFO)

        clog.info("iface %s up", "eth0\x1b[2K\x1b]0;pwned\x07")
        written = later.getvalue()
        leftover = [c for c in written if ord(c) < 32 and c != '\n']
        assert not leftover, f'control characters survived: {[hex(ord(c)) for c in leftover]}'
    finally:
        root.handlers = old


def test_a_traceback_keeps_its_newlines():
    """The mirror. Sanitising records must not flatten tracebacks - they are ours, not
    the caller's, and a one-line traceback helps nobody."""
    _root_buf, root, old = _isolated_root()
    try:
        _sanitizer()()
        buf = io.StringIO()
        h = logging.StreamHandler(buf)
        h.setFormatter(logging.Formatter('%(message)s'))
        lg = logging.getLogger('PegaProx_test_tb')
        lg.handlers = [h]
        lg.setLevel(logging.ERROR)
        try:
            1 / 0
        except ZeroDivisionError:
            lg.exception("boom")
        assert buf.getvalue().count('\n') > 2, 'the traceback was flattened'
    finally:
        root.handlers = old
