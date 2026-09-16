# -*- coding: utf-8 -*-
"""The toast has to show why something failed, not that it failed.

Reported from a migration that would not start. The server answered 409 with a sentence
explaining exactly what was wrong; the interface showed the word "Error" and nothing else.

The cause is one function signature and forty call sites that do not match it:

    const addToast = (message, type = 'success') => ...

    addToast('Saved', 'success')                       two arguments  — works
    addToast('Error', err.error, 'error')              three          — the reason lands
                                                                        in `type` and is
                                                                        dropped

Both shapes are read now. Read out of the source because the handler lives in a component
this suite cannot mount — and the alternative is no guard at all on a fault that made
every error in the product unreadable.
"""

import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(REPO, *parts), encoding='utf-8') as handle:
        return handle.read()


def test_add_toast_reads_the_three_argument_form():
    dashboard = _read('web', 'src', 'dashboard.js')
    start = dashboard.index('const addToast = ')
    body = dashboard[start:dashboard.index('const removeToast', start)]

    assert 'third' in body, (
        'addToast still takes two parameters, so every three-argument call loses its '
        'message into the type slot')
    assert 'title' in body and 'message' in body


def test_an_error_is_not_taken_away_before_it_can_be_read():
    dashboard = _read('web', 'src', 'dashboard.js')
    start = dashboard.index('const addToast = ')
    body = dashboard[start:dashboard.index('const removeToast', start)]
    assert "if (type === 'error') return;" in body, (
        'an error toast still disappears on a timer; it is the one kind somebody has to '
        'read and act on')

    ui = _read('web', 'src', 'ui.js')
    toast = ui[ui.index('function Toast('):]
    toast = toast[:toast.index('\n        }')]
    assert "if (type === 'error') return;" in toast
    assert 'onClose' in toast and 'aria-label="Dismiss"' in toast, (
        'an error that does not time out needs a way to be dismissed')


def test_the_component_renders_both_halves():
    ui = _read('web', 'src', 'ui.js')
    toast = ui[ui.index('function Toast('):]
    toast = toast[:toast.index('\n        }')]
    assert re.search(r'function Toast\(\{\s*title,\s*message', toast), (
        'the component no longer takes a title, so the three-argument calls lose it again')
    assert '{message}' in toast


def test_the_call_sites_that_prompted_this_are_still_the_common_shape():
    """Not a rule — a measurement, so the next person knows why both shapes are read.

    If this number ever drops to zero the compatibility can go; until then, rewriting
    forty call sites is a bigger change than reading two shapes.
    """
    three_args = 0
    web_src = os.path.join(REPO, 'web', 'src')
    for name in os.listdir(web_src):
        if name.endswith('.js'):
            three_args += len(re.findall(r'addToast\([^)]*,[^)]*,[^)]*\)',
                                         _read('web', 'src', name)))
    assert three_args > 10, (
        f'only {three_args} three-argument calls left — worth rewriting them and '
        f'simplifying addToast')
