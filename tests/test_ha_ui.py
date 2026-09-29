"""The warm standby in the web UI (#625).

Three pieces: the auth context keeps the `ha` object the server sends on login and
/auth/check, the settings modal gets a High Availability tab for admins, and every
layout shows a banner on a standby. The routes behind the panel are tested with the
API; these read the source and the bundle so the wiring cannot drift apart quietly.

Two traps the tests below hold shut: t() returns the key itself on a miss, so a
missing translation shows up raw instead of falling back, and the Tailwind build is
static, so a class that is not in static/css/tailwind.min.css simply does nothing.

The runtime tests at the end drive the built bundle in headless Chromium against a
fake server; they skip where Playwright is not installed.
LW
"""
import json
import os
import re
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'web', 'src')
LANGS = ['de', 'en', 'zh', 'pl', 'fr', 'es', 'pt', 'ko', 'it']


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding='utf-8') as fh:
        return fh.read()


@pytest.fixture(scope='module')
def ctx():
    return _read('web', 'src', 'contexts.js')


@pytest.fixture(scope='module')
def modal():
    return _read('web', 'src', 'settings_modal.js')


@pytest.fixture(scope='module')
def dash():
    return _read('web', 'src', 'dashboard.js')


@pytest.fixture(scope='module')
def cloud():
    return _read('web', 'src', 'cloud.js')


@pytest.fixture(scope='module')
def panel(modal):
    """Everything the HA section of settings_modal.js defines, from its header to the end."""
    return modal[modal.index('// PegaProx - High Availability (#625)'):]


@pytest.fixture(scope='module')
def banner(dash):
    start = dash.index('function HaStandbyBanner(')
    return dash[start:dash.index('function ClusterSidebarItem(', start)]


def _function(src, name):
    start = src.index(f'function {name}(')
    nxt = re.search(r'\n        function \w+\(', src[start + 1:])
    return src[start:start + 1 + nxt.start()] if nxt else src[start:]


# -- AuthContext ------------------------------------------------------------------

def test_the_context_keeps_ha_and_defaults_to_standalone(ctx):
    assert "const [ha, setHa] = useState({ role: 'standalone' });" in ctx
    provider = ctx[ctx.index('<AuthContext.Provider value={{'):]
    provider = provider[:provider.index('}}>')]
    assert ' ha,' in provider and 'refreshHa' in provider


def test_ha_comes_from_check_login_and_the_401(ctx):
    check = ctx[ctx.index('const checkSession = async'):ctx.index('const login = async')]
    assert 'applyHa(d.ha);' in check
    assert 'if (errData.ha_role) applyHa({ role: errData.ha_role });' in check
    login = ctx[ctx.index('const login = async'):ctx.index('const updatePreferences')]
    assert 'applyHa(data.ha);' in login


def test_anything_without_a_role_reads_as_standalone(ctx):
    start = ctx.index('const applyHa = (next) => {')
    body = ctx[start:ctx.index('\n            };', start)]
    assert "next.role) ? next : { role: 'standalone' }" in body


def test_only_a_standby_polls_for_its_sync_time(ctx):
    start = ctx.index('const h = setInterval(refreshHa, 30000);')
    effect = ctx[ctx.rindex('useEffect(', 0, start):ctx.index('}, [', start)]
    assert "if (!isAuthenticated || ha.role !== 'standby') return;" in effect


# -- settings modal -----------------------------------------------------------------

def test_the_panel_and_its_helpers_exist(panel):
    for name in ('function haRelTime(', 'function HaRoleBadge(', 'function HaRestartOverlay(',
                 'function HaPanel({ t, addToast, getAuthHeaders })'):
        assert name in panel, name


def test_the_tab_is_admin_gated(modal):
    component = _function(modal, 'PegaProxSettingsModal')
    assert 'const { getAuthHeaders, user: currentUser, isAdmin } = useAuth();' in component

    button_at = component.index("onClick={() => setActiveTab('ha')}")
    gate_at = component.rindex('{isAdmin && (', 0, button_at)
    # nothing but the button itself between the gate and the handler
    assert component[gate_at:button_at].count('<button') == 1
    assert "{t('pgHaTab')}" in component[button_at:component.index('</button>', button_at)]

    assert "{activeTab === 'ha' && isAdmin && (" in component
    mount = component[component.index("{activeTab === 'ha' && isAdmin && ("):]
    assert mount[:200].count('<HaPanel t={t} addToast={addToast} getAuthHeaders={getAuthHeaders} />') == 1


def test_the_modal_switches_to_the_tab_on_the_banner_event(modal):
    component = _function(modal, 'PegaProxSettingsModal')
    assert "const toHa = () => setActiveTab('ha');" in component
    assert "window.addEventListener('pegaprox-navigate-ha', toHa);" in component
    assert "window.removeEventListener('pegaprox-navigate-ha', toHa);" in component


def test_the_panel_speaks_the_agreed_contract(panel):
    body = _function(panel, 'HaPanel')
    assert '`${API_URL}/ha/status`' in body
    assert "send('POST', 'pairing-code', withPassword('code', { url }))" in body
    assert "send('POST', 'join', withPassword('join', { code: joinCode.trim(), own_url: url, confirm: true }))" in body
    assert "send('POST', 'sync-now'" in body
    assert "send('PUT', 'settings', { interval: n })" in body
    assert "const WORD = { promote: 'PROMOTE', unpair: 'UNPAIR' };" in body
    assert "send('POST', what, withPassword('confirm', { confirm: WORD[what] }))" in body
    # the server's own error text, not a fixed string, and its code next to it
    assert 'await PegaProxApiErrors.message(r, fallback' in body
    assert "const code = (await r.clone().json().catch(() => null))?.code || '';" in body


def test_typed_confirmation_is_exact(panel):
    body = _function(panel, 'HaPanel')
    assert "disabled={typed !== WORD[confirmAction] || needsPassword('confirm') || !!busy}" in body


def test_join_needs_the_checkbox_and_a_code(panel):
    body = _function(panel, 'HaPanel')
    assert "disabled={!joinConfirm || !joinCode.trim() || needsPassword('join') || !!busy}" in body
    assert "t('pgHaJoinWarning')" in body


# -- re-authentication (#625 review) ------------------------------------------------------

def test_every_handover_action_asks_for_the_password(panel):
    """Pairing code, join, promote and unpair each carry the account password."""
    body = _function(panel, 'HaPanel')
    assert "const withPassword = (form, body) => sso ? body : { ...body, user_password: passwords[form] };" in body
    for form in ('code', 'join', 'confirm'):
        assert body.count(f"withPassword('{form}', ") == 1, form
        assert f"passwordInput('{form}', 'pgha-{form}-password')" in body, form
        assert f"reauthNote('{form}', 'pgha-{form}-password')" in body, form
        assert f"needsPassword('{form}')" in body, form
    # promote and unpair share the typed box, and so its password field
    typed_box = body[body.index('const typedBox = '):body.index('if (!status) {')]
    assert "passwordInput('confirm', 'pgha-confirm-password')" in typed_box
    # the pairing-code button waits for the password too
    assert "<button onClick={createCode} disabled={needsPassword('code') || !!busy}" in body
    # the field itself: a current password, kept away from password managers like the
    # root password fields of the auto-install wizard
    field = body[body.index('const passwordInput = (form, id) =>'):body.index('const reauthNote = ')]
    for attr in ('type="password"', 'autoComplete="current-password"', 'data-lpignore="true"',
                 'data-1p-ignore="true"', 'data-bwignore="true"', "{t('pgHaPassword')}"):
        assert attr in field, attr


def test_sso_accounts_type_no_password(panel):
    body = _function(panel, 'HaPanel')
    assert "const sso = ['oidc', 'entra'].includes(user?.auth_source);" in body
    assert "const needsPassword = (form) => !sso && !passwords[form];" in body
    assert "const passwordInput = (form, id) => !sso && (" in body
    assert 'const { refreshHa, user, logout } = useAuth();' in body


def test_a_refused_reauth_stays_at_its_field(panel):
    body = _function(panel, 'HaPanel')
    refused = body[body.index('const reauthRefused = (form, res) => {'):]
    refused = refused[:refused.index('\n            };')]
    assert "res.code !== 'HA_REAUTH' && res.code !== 'HA_REAUTH_RECENT'" in refused
    assert 'setReauth({ form, code: res.code, error: res.error });' in refused
    assert "setPassword(form, '');" in refused
    # each action hands a refusal to it before its usual error path, and a refusal
    # returns before the box closes
    assert "if (!res.ok) { if (!reauthRefused('code', res)) addToast?.(res.error, 'error'); return; }" in body
    assert "if (!res.ok) { if (!reauthRefused('join', res)) setJoinError(res.error); return; }" in body
    assert "if (!res.ok) { if (!reauthRefused('confirm', res)) addToast?.(res.error, 'error'); return; }" in body
    # a stale SSO sign-in gets a way to sign in again
    note = body[body.index('const reauthNote = '):body.index('const peerCard = (')]
    assert "reauth.code === 'HA_REAUTH_RECENT'" in note
    assert 'onClick={() => logout()}' in note
    assert "{t('pgHaSignInAgain')}" in note


def test_a_broken_state_file_hides_promote_and_locks_the_interval(panel):
    body = _function(panel, 'HaPanel')
    assert 'const broken = !!status?.broken;' in body
    standby = body[body.index("{role === 'standby' && ("):]
    promote_at = standby.index("openConfirm('promote')")
    assert standby.rindex('{!broken && (', 0, promote_at) > standby.rindex('<button onClick={syncNow}', 0, promote_at)
    assert "const typedBox = confirmAction && !(confirmAction === 'promote' && broken) && (" in body
    interval = body[body.index('const intervalCard = ('):body.index('const typedBox = ')]
    assert 'value={interval} disabled={broken}' in interval
    assert 'disabled={!!busy || broken}' in interval
    note = body[body.index('{broken && ('):]
    note = note[:note.index('</div>\n                        )}')]
    assert "t('pgHaBroken')" in note and "t('pgHaBrokenLocked')" in note


def test_roles_render_their_own_cards(panel):
    body = _function(panel, 'HaPanel')
    standalone = body[body.index("{role === 'standalone' && ("):body.index("{role === 'active' && (")]
    active = body[body.index("{role === 'active' && ("):body.index("{role === 'standby' && (")]
    standby = body[body.index("{role === 'standby' && ("):]
    assert "t('pgHaMakeActiveTitle')" in standalone and "t('pgHaJoinTitle')" in standalone
    assert '{peerCard}' in active and '{intervalCard}' in active and "openConfirm('unpair')" in active
    assert "openConfirm('promote')" not in active
    for needle in ('{peerCard}', 'onClick={syncNow}', "openConfirm('promote')", "openConfirm('unpair')",
                   "t('pgHaSkippedColumns')"):
        assert needle in standby, needle


def test_a_restart_blocks_the_page_and_reloads(panel):
    overlay = _function(panel, 'HaRestartOverlay')
    assert '`${API_URL}/auth/check?t=${Date.now()}`' in overlay
    assert 'setTimeout(tick, 2000)' in overlay
    assert 'elapsed >= 120000' in overlay
    assert 'window.location.reload()' in overlay
    body = _function(panel, 'HaPanel')
    assert "setRestarting('standby')" in body
    assert "setRestarting(what === 'promote' ? 'active' : 'standalone')" in body
    assert '{restarting && <HaRestartOverlay t={t} expectRole={restarting} />}' in body


# -- banner ---------------------------------------------------------------------------

def test_the_banner_shows_on_a_standby_only(banner):
    assert "const standby = ha?.role === 'standby';" in banner
    assert 'if (!standby) return null;' in banner
    assert "t('pgHaBannerStandby')" in banner
    assert ".replace('{url}', ha.peer_url || '-')" in banner
    # the HA tab button is for admins
    assert 'const button = isAdmin && onOpenHa && (' in banner


def test_modern_and_corporate_render_it_next_to_the_password_banner(dash):
    main = dash[dash.index('function PegaProxDashboard('):]
    at = main.index('<PasswordExpiryBanner onChangePassword={() => setShowProfile(true)} />')
    assert main[at:at + 200].count('<HaStandbyBanner onOpenHa={openHaSettings} />') == 1
    # Modern and Corporate share this return; the cloud branch returns before it
    assert main.index('if (isCloud) {') < at
    opener = main[main.index('const openHaSettings = () => {'):]
    opener = opener[:opener.index('};')]
    assert 'setShowSettings(true);' in opener
    assert "new CustomEvent('pegaprox-navigate-ha')" in opener


def test_cloud_renders_it_in_the_shell(cloud):
    shell = _function(cloud, 'CloudShell')
    topbar = shell.index('<CloudTopbar')
    at = shell.index('<HaStandbyBanner cloud onOpenHa={() => {')
    scroll = shell.index('<div className="cloud-content-scroll">')
    assert topbar < at < scroll
    assert "new CustomEvent('pegaprox-navigate-ha')" in shell[at:scroll]
    assert 'onOpenSettings && onOpenSettings();' in shell[at:scroll]


# -- translations -----------------------------------------------------------------------

def _used_keys():
    keys = set()
    for name in ('settings_modal.js', 'dashboard.js', 'cloud.js'):
        keys.update(re.findall(r"t\('(pgHa\w+)'\)", _read('web', 'src', name)))
    return sorted(keys)


def _blocks():
    src = _read('web', 'src', 'translations.js')
    starts = sorted((m.start(), m.group(1)) for m in re.finditer(r'^ {12}([a-z]{2}): \{$', src, re.M))
    assert [lang for _, lang in sorted(starts, key=lambda s: LANGS.index(s[1]))] == LANGS
    out = {}
    for i, (pos, lang) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(src)
        out[lang] = src[pos:end]
    return out


def test_the_panel_uses_its_own_keys():
    # the Proxmox HA strings already use ha*; ours are pgHa* so nothing collides
    assert len(_used_keys()) >= 50


@pytest.mark.parametrize('lang', LANGS)
def test_every_new_key_exists_once_per_language(lang):
    block = _blocks()[lang]
    for key in _used_keys():
        n = len(re.findall(r'^ +%s: ' % key, block, re.M))
        assert n == 1, f'{key} appears {n} times in {lang}'


def test_no_key_is_defined_that_nothing_uses():
    used = set(_used_keys())
    for lang, block in _blocks().items():
        defined = set(re.findall(r'^ +(pgHa\w+): ', block, re.M))
        assert defined == used, (lang, sorted(defined ^ used))


def test_placeholders_survive_translation():
    blocks = _blocks()
    for key in _used_keys():
        en = re.search(r'^ +%s: (.*),$' % key, blocks['en'], re.M).group(1)
        for lang, block in blocks.items():
            value = re.search(r'^ +%s: (.*),$' % key, block, re.M).group(1)
            assert sorted(re.findall(r'\{\w+\}', value)) == sorted(re.findall(r'\{\w+\}', en)), (lang, key)


def test_no_em_dash_in_the_new_code(panel, banner):
    new_strings = [line for block in _blocks().values() for line in block.splitlines() if 'pgHa' in line]
    for text in [panel, banner] + new_strings:
        assert '\u2014' not in text


# -- styling ------------------------------------------------------------------------------

def _classes(block):
    names = set()
    for m in re.finditer(r'className=(?:"([^"]*)"|\{`([^`]*)`\})', block):
        txt = m.group(1) if m.group(1) is not None else m.group(2)
        txt = re.sub(r'\$\{[^}]*\}', ' ', txt)
        names.update(n for n in txt.split() if re.match(r'^[a-z]', n))
    for m in re.finditer(r"'([a-z][a-z0-9:/\-\.\[\] ]*)'", block):
        parts = m.group(1).split()
        if parts and any(re.match(r'^(bg|text|border|px|py|rounded|flex)-?', p) for p in parts):
            names.update(parts)
    return names


def test_every_class_is_in_the_static_tailwind_build(panel, banner, modal):
    css = _read('static', 'css', 'tailwind.min.css') + _read('web', 'index.html.original')
    have = {m.group(1).replace('\\', '') for m in re.finditer(r'\.((?:\\.|[A-Za-z0-9_-])+)', css)}
    tab = modal[modal.index("onClick={() => setActiveTab('ha')}"):]
    tab = tab[:tab.index('</button>')]
    names = _classes(panel) | _classes(banner) | _classes(tab)
    # JS names that sit inside ${...} or class-string constants
    names -= {'card', 'field', 'input', 'btn', 'btnGhost'}
    missing = sorted(n for n in names if n not in have)
    assert not missing, f'not in static/css/tailwind.min.css: {missing}'


# -- bundle ----------------------------------------------------------------------------------

def test_the_bundle_was_rebuilt():
    """web/index.html is generated from web/src; a source-only change ships nothing."""
    bundle = _read('web', 'index.html')
    for needle in ('function HaPanel(', 'function HaStandbyBanner(', 'function HaRestartOverlay(',
                   'pegaprox-navigate-ha', "setActiveTab('ha')", 'refreshHa'):
        assert needle in bundle, needle
    for key in _used_keys():
        assert key in bundle, key


# -- runtime: the built bundle in a real browser ----------------------------------------------
#
# A compile proves nothing about what renders. These load web/index.html in headless
# Chromium with every request intercepted: / is the bundle, /static/* comes from the
# checkout, /api/* is answered below. Skipped where Playwright or its Chromium is not
# installed (CI installs neither).

PEER = 'https://pegaprox-a.example:5000'
SELF = 'https://pegaprox-b.example:5000'
BASE = 'http://pegaprox.test'


def _iso_ago(sec):
    return time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime(time.time() - sec))


PASSWORD = 'correct horse'


class _FakeServer:
    """Just enough of the HA contract, /auth/check and a restart that takes a few seconds.

    Pairing code, join, promote and unpair want the account password again, the way
    the server does: a local account sends user_password, an SSO account sends nothing
    and is refused when its sign-in is too old (sso_stale).
    """

    def __init__(self, role='standby', layout='modern', language='en', admin=True,
                 auth_source='local', broken='', sso_stale=False):
        self.role, self.layout, self.language, self.admin = role, layout, language, admin
        self.auth_source, self.broken, self.sso_stale = auth_source, broken, sso_stale
        self.down_until = 0.0
        self.role_after_restart = None
        self.calls = []
        self.bodies = {}
        self.fail_pairing_once = False
        self.interval = 30
        self.logged_out = False

    def _reauth_refusal(self, body):
        if self.auth_source in ('oidc', 'entra'):
            if self.sso_stale:
                return {'error': 'Your sign-in is older than 10 minutes. Sign in again, then retry.',
                        'code': 'HA_REAUTH_RECENT'}
            return None
        if body.get('user_password') != PASSWORD:
            return {'error': 'Incorrect password', 'code': 'HA_REAUTH'}
        return None

    def status(self):
        peer = None
        if self.role in ('active', 'standby'):
            peer = {'instance_id': 'b' * 32, 'url': PEER if self.role == 'standby' else SELF,
                    'fingerprint': '', 'paired_at': _iso_ago(3600),
                    'role_seen': 'active' if self.role == 'standby' else 'standby',
                    'epoch_seen': 2, 'last_contact': _iso_ago(12),
                    'last_error': '' if self.role == 'standby' else 'Cannot reach the peer: ConnectTimeout'}
        sync = {}
        if self.role == 'standby':
            sync = {'last_ok_at': _iso_ago(20), 'last_attempt_at': _iso_ago(20), 'last_error': '',
                    'rows': 1234, 'tables': 41, 'source_epoch': 2, 'etag': None,
                    'skipped_columns': {'clusters': ['new_col']}}
        return {'role': self.role, 'epoch': 0 if self.role == 'standalone' else 2,
                'instance_id': 'a' * 32, 'interval': self.interval, 'broken': self.broken,
                'pairing_open_until': None, 'peer': peer, 'sync': sync,
                'suggested_url': SELF, 'own_fingerprint': ''}

    def banner(self):
        if self.role != 'standby':
            return {'role': self.role}
        return {'role': 'standby', 'peer_url': PEER, 'last_sync_at': _iso_ago(90)}

    def _restart(self, role):
        self.down_until = time.time() + 4
        self.role_after_restart = role

    def handle(self, route):
        req = route.request
        path = re.sub(r'^https?://[^/]+', '', req.url).split('?')[0]
        if not req.url.startswith(BASE):
            return route.abort()
        if path in ('/', '/index.html'):
            return route.fulfill(status=200, body=_read('web', 'index.html'),
                                 headers={'Content-Type': 'text/html; charset=utf-8'})
        if path.startswith('/static/'):
            fp = os.path.join(ROOT, path.lstrip('/'))
            if os.path.isfile(fp):
                ctype = 'text/css' if fp.endswith('.css') else 'application/javascript'
                with open(fp, 'rb') as fh:
                    return route.fulfill(status=200, body=fh.read(), headers={'Content-Type': ctype})
            return route.fulfill(status=404, body='')
        if not path.startswith('/api/'):
            return route.fulfill(status=404, body='')
        self.calls.append((req.method, path))
        try:
            body = json.loads(req.post_data) if req.post_data else {}
        except Exception:
            body = {}
        self.bodies.setdefault(path, []).append(body)

        def answer(data, status=200):
            return route.fulfill(status=status, body=json.dumps(data),
                                 headers={'Content-Type': 'application/json'})

        if path == '/api/auth/check':
            if time.time() < self.down_until:
                return route.abort('connectionrefused')
            if self.logged_out:
                return answer({'authenticated': False, 'ha_role': self.role})
            if self.role_after_restart:
                self.role, self.role_after_restart = self.role_after_restart, None
            user = {'username': 'admin' if self.admin else 'viewer',
                    'role': 'admin' if self.admin else 'viewer', 'display_name': 'Admin',
                    'ui_layout': self.layout, 'layout_chosen': True, 'theme': '',
                    'language': self.language, 'permissions': [], 'enabled': True,
                    'auth_source': self.auth_source}
            return answer({'authenticated': True, 'session_id': 'sid', 'user': user,
                           'ha': self.banner(), 'default_theme': 'proxmoxDark'})
        if path == '/api/auth/logout':
            self.logged_out = True
            return answer({'success': True})
        if path == '/api/ha/status':
            return answer(self.status())
        if path in ('/api/ha/pairing-code', '/api/ha/join', '/api/ha/promote', '/api/ha/unpair'):
            refusal = self._reauth_refusal(body)
            if refusal:
                return answer(refusal, 403)
        if path == '/api/ha/settings' and self.broken:
            return answer({'error': 'The HA state file cannot be read - repair or remove it first'}, 409)
        if path == '/api/ha/pairing-code':
            if self.fail_pairing_once:
                self.fail_pairing_once = False
                return answer({'error': 'This instance is already paired - unpair it first'}, 409)
            return answer({'code': 'pgxha1_' + 'Q' * 120, 'expires_at': int(time.time()) + 900})
        if path == '/api/ha/join':
            if body.get('confirm') is not True or not body.get('code'):
                return answer({'error': 'confirm missing'}, 400)
            self._restart('standby')
            return answer({'success': True, 'restarting': True})
        if path == '/api/ha/sync-now':
            return answer({'result': 'applied', 'status': self.status()})
        if path == '/api/ha/settings':
            self.interval = int(body.get('interval') or 30)
            return answer({'success': True, 'interval': self.interval})
        if path == '/api/ha/promote':
            if body.get('confirm') != 'PROMOTE':
                return answer({'error': 'type PROMOTE'}, 400)
            self._restart('active')
            return answer({'success': True, 'epoch': 3, 'restarting': True})
        if path == '/api/ha/unpair':
            if body.get('confirm') != 'UNPAIR':
                return answer({'error': 'type UNPAIR'}, 400)
            was = self.role
            if was == 'standby':
                self._restart('standalone')
            else:
                self.role = 'standalone'
            return answer({'success': True, 'restarting': was == 'standby'})
        if req.method != 'GET' and self.role == 'standby':
            return answer({'error': 'This is a standby instance. Make changes on the active instance; '
                                    'they arrive here with the next sync.', 'code': 'HA_STANDBY'}, 409)
        return answer({'error': 'not mocked'}, 404)


@pytest.fixture(scope='module')
def browser():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip('Playwright is not installed')
    try:
        pw = sync_playwright().start()
    except Exception as e:
        pytest.skip(f'Playwright does not start here: {e}')
    try:
        br = pw.chromium.launch(headless=True)
    except Exception as e:
        pw.stop()
        pytest.skip(f'no Chromium for Playwright: {e}')
    yield br
    br.close()
    pw.stop()


class _App:
    def __init__(self, browser, server):
        self.server = server
        self.ctx = browser.new_context(viewport={'width': 1600, 'height': 1000})
        # the monthly sponsor modal for admins would sit on top of everything
        self.ctx.add_init_script("""try {
            for (const u of ['admin', 'viewer']) localStorage.setItem('pegaprox_sponsor_v2:' + u, String(Date.now() + 1e10));
            localStorage.setItem('pegaprox-shortcuts-hint-shown', '1');
        } catch (e) {}""")
        self.page = self.ctx.new_page()
        self.errors = []
        self.loads = []
        self.page.on('pageerror', lambda e: self.errors.append(f'pageerror: {e}'))
        self.page.on('console', self._console)
        self.page.on('load', lambda: self.loads.append(time.time()))
        self.page.route('**/*', server.handle)
        self.page.goto(BASE + '/', wait_until='load')
        self.wait_for_app()

    def _console(self, msg):
        # mocked 404s and the refused connections of the simulated restart
        if msg.type == 'error' and 'Failed to load resource' not in msg.text and 'net::ERR' not in msg.text:
            self.errors.append(f'console: {msg.text[:300]}')

    def wait_for_app(self):
        self.page.wait_for_function(
            '() => document.querySelector("header") || document.querySelector(".cloud-shell")', timeout=30000)
        self.page.wait_for_timeout(500)

    def wait_for_reload(self, before, timeout=25):
        deadline = time.time() + timeout
        while time.time() < deadline and len(self.loads) == before:
            self.page.wait_for_timeout(250)
        assert len(self.loads) > before, 'the page did not reload after the restart'
        self.wait_for_app()

    def open_settings(self):
        # "g ," is the settings shortcut in Modern and Corporate
        self.page.locator('body').click(position={'x': 5, 'y': 400})
        self.page.keyboard.press('g')
        self.page.keyboard.press(',')
        self.page.get_by_text('PegaProx Settings').first.wait_for(timeout=5000)

    def see(self, text, timeout=5000):
        self.page.get_by_text(text).first.wait_for(timeout=timeout)


@pytest.fixture
def open_app(browser):
    apps = []

    def _open(**kw):
        app = _App(browser, _FakeServer(**kw))
        apps.append(app)
        return app
    yield _open
    for app in apps:
        app.ctx.close()


def test_runtime_standby_in_modern_banner_panel_sync_and_promote(open_app):
    app = open_app(role='standby', layout='modern')
    page = app.page
    banner = page.locator('[data-ha-banner="classic"]')
    assert banner.is_visible()
    text = banner.inner_text()
    assert f'Standby instance, synced from {PEER}, last sync' in text
    assert 'minute' in text, text
    # above the header, where the password expiry banner sits
    assert page.evaluate('() => !!(document.querySelector("[data-ha-banner]").compareDocumentPosition('
                         'document.querySelector("header")) & Node.DOCUMENT_POSITION_FOLLOWING)')

    banner.get_by_role('button', name='High Availability').click()
    panel = page.locator('[data-ha-role="standby"]')
    panel.wait_for(timeout=5000)
    body = panel.inner_text()
    for needle in ('Standby', PEER, 'Last successful sync', '1234 rows in 41 tables', 'clusters: new_col',
                   'Sync now', 'Promote to active', 'Unpair'):
        assert needle in body, needle

    page.get_by_role('button', name='Sync now').click()
    app.see('Configuration synced from the active instance')
    assert ('POST', '/api/ha/sync-now') in app.server.calls

    page.get_by_role('button', name='Promote to active').click()
    assert 'steps down to standby as soon as it sees this one' in panel.inner_text()
    confirm = panel.locator('button', has_text='Promote to active').last
    assert confirm.is_disabled()
    page.fill('#pgha-typed', 'promote')
    assert confirm.is_disabled()
    page.fill('#pgha-typed', 'PROMOTE')
    assert confirm.is_disabled(), 'promote must wait for the password'
    page.fill('#pgha-confirm-password', PASSWORD)
    assert confirm.is_enabled()
    before = len(app.loads)
    confirm.click()
    app.see('Restarting PegaProx...', timeout=3000)
    assert app.server.bodies['/api/ha/promote'] == [{'confirm': 'PROMOTE', 'user_password': PASSWORD}]
    # the fake instance is gone for 4 s and comes back active
    app.wait_for_reload(before)
    assert page.locator('[data-ha-banner]').count() == 0
    assert not app.errors, app.errors


@pytest.mark.parametrize('layout,kind', [('corporate', 'classic'), ('cloud', 'cloud')])
def test_runtime_standby_banner_in_the_other_layouts(open_app, layout, kind):
    app = open_app(role='standby', layout=layout)
    banner = app.page.locator(f'[data-ha-banner="{kind}"]')
    assert banner.is_visible()
    assert PEER in banner.inner_text()
    if layout == 'cloud':
        assert app.page.locator('.cloud-content [data-ha-banner="cloud"]').count() == 1
    banner.get_by_role('button', name='High Availability').click()
    app.page.locator('[data-ha-role="standby"]').wait_for(timeout=5000)
    assert not app.errors, app.errors


def test_runtime_a_viewer_gets_the_banner_but_no_button_and_no_tab(open_app):
    app = open_app(role='standby', layout='modern', admin=False)
    banner = app.page.locator('[data-ha-banner="classic"]')
    assert banner.is_visible()
    assert banner.locator('button').count() == 0
    app.open_settings()
    assert app.page.locator('button', has_text='High Availability').count() == 0
    assert not app.errors, app.errors


def test_runtime_the_banner_speaks_german(open_app):
    app = open_app(role='standby', layout='modern', language='de')
    text = app.page.locator('[data-ha-banner="classic"]').inner_text()
    assert f'Standby-Instanz, synchronisiert von {PEER}, letzte Synchronisierung vor' in text, text
    assert app.page.locator('[data-ha-banner] button', has_text='Hochverfügbarkeit').count() == 1
    assert not app.errors, app.errors


def test_runtime_standalone_pairing_code_and_join(open_app):
    app = open_app(role='standalone', layout='modern')
    app.server.fail_pairing_once = True
    page = app.page
    assert page.locator('[data-ha-banner]').count() == 0
    app.open_settings()
    page.locator('button', has_text='High Availability').first.click()
    panel = page.locator('[data-ha-role="standalone"]')
    panel.wait_for(timeout=5000)
    assert 'Make this the active instance' in panel.inner_text()
    assert page.input_value('#pgha-own-url') == SELF
    assert page.input_value('#pgha-join-url') == SELF

    create = page.get_by_role('button', name='Create pairing code')
    assert create.is_disabled(), 'the pairing code must wait for the password'
    page.fill('#pgha-code-password', PASSWORD)
    assert create.is_enabled()

    # a refusal shows the server's words, and keeps the password for the retry
    create.click()
    app.see('This instance is already paired - unpair it first')
    assert page.input_value('#pgha-code-password') == PASSWORD

    create.click()
    box = page.locator('[data-ha-code]')
    box.wait_for(timeout=3000)
    assert app.server.bodies['/api/ha/pairing-code'][-1] == {'url': SELF, 'user_password': PASSWORD}
    assert page.input_value('#pgha-code-password') == '', 'the password outlives its use'
    first = box.inner_text()
    assert 'pgxha1_' in first and 'shown only once' in first
    assert re.search(r'Expires in 1[45]:\d\d', first), first
    assert box.locator('button[title="Copy"]').count() == 1
    page.wait_for_timeout(1300)
    assert box.inner_text() != first, 'the countdown does not tick'

    join = page.get_by_role('button', name='Pair as standby')
    assert join.is_disabled()
    page.fill('#pgha-join-code', 'pgxha1_' + 'Z' * 40)
    assert join.is_disabled(), 'join must wait for the checkbox'
    panel.locator('input[type="checkbox"]').check()
    assert join.is_disabled(), 'join must wait for the password'
    page.fill('#pgha-join-password', PASSWORD)
    assert join.is_enabled()
    before = len(app.loads)
    join.click()
    app.see('Restarting PegaProx...', timeout=3000)
    assert app.server.calls.count(('POST', '/api/ha/join')) == 1
    assert app.server.bodies['/api/ha/join'] == [{'code': 'pgxha1_' + 'Z' * 40, 'own_url': SELF,
                                                  'confirm': True, 'user_password': PASSWORD}]
    app.wait_for_reload(before)
    assert page.locator('[data-ha-banner="classic"]').is_visible()
    assert not app.errors, app.errors


def test_runtime_active_interval_and_unpair(open_app):
    app = open_app(role='active', layout='modern')
    page = app.page
    app.open_settings()
    page.locator('button', has_text='High Availability').first.click()
    panel = page.locator('[data-ha-role="active"]')
    panel.wait_for(timeout=5000)
    body = panel.inner_text()
    for needle in (SELF, 'Last contact', 'ConnectTimeout', 'Interval in seconds', 'Unpair'):
        assert needle in body, needle
    assert 'Promote to active' not in body

    assert page.input_value('#pgha-interval') == '30'
    page.fill('#pgha-interval', '2')
    panel.get_by_role('button', name='Save').click()
    app.see('The interval must be between 5 and 3600 seconds')
    assert ('PUT', '/api/ha/settings') not in app.server.calls
    page.fill('#pgha-interval', '45')
    panel.get_by_role('button', name='Save').click()
    app.see('Interval saved')
    assert app.server.interval == 45

    panel.get_by_role('button', name='Unpair').first.click()
    assert 'The standby stops receiving changes' in panel.inner_text()
    page.fill('#pgha-typed', 'UNPAIR')
    page.fill('#pgha-confirm-password', PASSWORD)
    panel.locator('button', has_text='Unpair').last.click()
    # an active that unpairs does not restart: the panel just turns standalone
    page.locator('[data-ha-role="standalone"]').wait_for(timeout=5000)
    assert page.get_by_text('Restarting PegaProx...').count() == 0
    assert not app.errors, app.errors


# -- runtime: re-authentication and the unreadable state file --------------------------------

def _open_ha(app, role):
    if role == 'standby':
        app.page.locator('[data-ha-banner="classic"]').get_by_role('button', name='High Availability').click()
    else:
        app.open_settings()
        app.page.locator('button', has_text='High Availability').first.click()
    panel = app.page.locator(f'[data-ha-role="{role}"]')
    panel.wait_for(timeout=5000)
    return panel


def _follows(page, first, second):
    return page.evaluate('([a, b]) => !!(document.querySelector(a).compareDocumentPosition('
                         'document.querySelector(b)) & Node.DOCUMENT_POSITION_FOLLOWING)', [first, second])


def test_runtime_a_wrong_password_keeps_the_promote_box_open(open_app):
    app = open_app(role='standby', layout='modern')
    page = app.page
    panel = _open_ha(app, 'standby')
    page.get_by_role('button', name='Promote to active').click()
    confirm = panel.locator('button', has_text='Promote to active').last
    page.fill('#pgha-typed', 'PROMOTE')
    page.fill('#pgha-confirm-password', 'wrong')
    confirm.click()

    note = panel.locator('[data-ha-reauth="HA_REAUTH"]')
    note.wait_for(timeout=3000)
    assert 'Incorrect password' in note.inner_text()
    # under the password field, and not as a toast on top
    assert _follows(page, '#pgha-confirm-password', '[data-ha-reauth]')
    assert page.get_by_text('Incorrect password').count() == 1
    # the box stays, the word stays, the password is gone and the button waits for a new one
    assert page.input_value('#pgha-typed') == 'PROMOTE'
    assert page.input_value('#pgha-confirm-password') == ''
    assert page.get_attribute('#pgha-confirm-password', 'aria-invalid') == 'true'
    assert confirm.is_disabled()
    assert page.get_by_text('Restarting PegaProx...').count() == 0
    assert app.server.bodies['/api/ha/promote'] == [{'confirm': 'PROMOTE', 'user_password': 'wrong'}]

    page.fill('#pgha-confirm-password', PASSWORD)
    confirm.click()
    app.see('Restarting PegaProx...', timeout=3000)
    assert app.server.bodies['/api/ha/promote'][-1] == {'confirm': 'PROMOTE', 'user_password': PASSWORD}
    assert not app.errors, app.errors


def test_runtime_a_wrong_password_shows_at_the_card_that_sent_it(open_app):
    app = open_app(role='standalone', layout='modern')
    page = app.page
    panel = _open_ha(app, 'standalone')

    page.fill('#pgha-code-password', 'wrong')
    page.get_by_role('button', name='Create pairing code').click()
    note = panel.locator('[data-ha-reauth="HA_REAUTH"]')
    note.wait_for(timeout=3000)
    assert _follows(page, '#pgha-code-password', '[data-ha-reauth]')
    assert not _follows(page, '#pgha-join-password', '[data-ha-reauth]')
    assert page.input_value('#pgha-code-password') == ''
    assert page.locator('[data-ha-code]').count() == 0

    page.fill('#pgha-join-code', 'pgxha1_' + 'Z' * 40)
    panel.locator('input[type="checkbox"]').check()
    page.fill('#pgha-join-password', 'wrong too')
    join = page.get_by_role('button', name='Pair as standby')
    join.click()
    page.wait_for_function('() => document.querySelector("#pgha-join-password").value === ""', timeout=3000)
    # one note, now at the join card; the join's own error box stays empty
    assert panel.locator('[data-ha-reauth]').count() == 1
    assert _follows(page, '#pgha-join-password', '[data-ha-reauth]')
    assert page.get_attribute('#pgha-code-password', 'aria-invalid') is None
    assert page.input_value('#pgha-join-code') == 'pgxha1_' + 'Z' * 40
    assert join.is_disabled()
    assert page.get_by_text('Restarting PegaProx...').count() == 0
    assert [b.get('user_password') for b in app.server.bodies['/api/ha/join']] == ['wrong too']
    assert not app.errors, app.errors


@pytest.mark.parametrize('auth_source', ['oidc', 'entra'])
def test_runtime_an_sso_account_types_no_password(open_app, auth_source):
    app = open_app(role='standalone', layout='modern', auth_source=auth_source)
    page = app.page
    panel = _open_ha(app, 'standalone')
    assert panel.locator('input[type="password"]').count() == 0
    assert page.locator('#pgha-code-password, #pgha-join-password').count() == 0

    create = page.get_by_role('button', name='Create pairing code')
    assert create.is_enabled()
    create.click()
    page.locator('[data-ha-code]').wait_for(timeout=3000)
    assert app.server.bodies['/api/ha/pairing-code'] == [{'url': SELF}]

    page.fill('#pgha-join-code', 'pgxha1_' + 'Z' * 40)
    panel.locator('input[type="checkbox"]').check()
    assert page.get_by_role('button', name='Pair as standby').is_enabled()
    assert not app.errors, app.errors


def test_runtime_a_stale_sso_sign_in_offers_to_sign_in_again(open_app):
    app = open_app(role='active', layout='modern', auth_source='oidc', sso_stale=True)
    page = app.page
    panel = _open_ha(app, 'active')
    panel.get_by_role('button', name='Unpair').first.click()
    assert page.locator('#pgha-confirm-password').count() == 0
    page.fill('#pgha-typed', 'UNPAIR')
    confirm = panel.locator('button', has_text='Unpair').last
    assert confirm.is_enabled()
    confirm.click()

    note = panel.locator('[data-ha-reauth="HA_REAUTH_RECENT"]')
    note.wait_for(timeout=3000)
    assert 'older than 10 minutes' in note.inner_text()
    assert app.server.bodies['/api/ha/unpair'] == [{'confirm': 'UNPAIR'}]
    assert page.input_value('#pgha-typed') == 'UNPAIR', 'the box closed on a refusal'
    assert app.server.role == 'active'

    note.get_by_role('button', name='Sign in again').click()
    page.wait_for_function('() => !document.querySelector("[data-ha-role]")', timeout=5000)
    assert ('POST', '/api/auth/logout') in app.server.calls
    assert not app.errors, app.errors


def test_runtime_an_unreadable_state_file_offers_no_promote(open_app):
    reason = "Expecting ',' delimiter: line 1 column 244 (char 243)"
    app = open_app(role='standby', layout='modern', broken=reason)
    page = app.page
    panel = _open_ha(app, 'standby')
    note = panel.locator('[data-ha-broken]')
    assert 'The HA state file cannot be read' in note.inner_text()
    assert reason in note.inner_text()
    assert 'Promoting and changing the interval stay locked' in note.inner_text()

    assert panel.locator('button', has_text='Promote to active').count() == 0
    assert page.locator('#pgha-interval').is_disabled()
    save = page.locator('#pgha-interval + button')
    assert save.inner_text() == 'Save' and save.is_disabled()
    # sync and unpair stay
    assert panel.get_by_role('button', name='Sync now').is_enabled()
    assert panel.get_by_role('button', name='Unpair').is_enabled()
    assert ('PUT', '/api/ha/settings') not in app.server.calls
    assert not app.errors, app.errors


def test_a_standby_says_why_its_cluster_list_is_empty():
    """(#625 live test) A standby starts no cluster managers, so its list is empty on
    purpose. It used to say "No clusters configured" and offer Add First Cluster and
    the automated install, both refused there. Sidebar card, Corporate and Modern
    overview."""
    dash = _read('web', 'src', 'dashboard.js')
    card = dash[dash.index("{t('noClusterSelected')}") - 400:dash.index("{t('noClusterSelected')}")]
    assert "haStandby ? (" in card and "t('pgHaNoClustersHere')" in card
    assert '{canAutoInstall && !haStandby && (' in dash

    vm = _read('web', 'src', 'vm_modals.js')
    start = vm.index('function AllClustersOverview(')
    body = vm[start:vm.index('function GroupSettingsModal(', start)]
    assert "const haStandby = ((useAuth() || {}).ha || {}).role === 'standby';" in body
    assert body.count("t('pgHaNoClustersHere')") == 2
    assert body.count('{onAutoInstall && !haStandby && (') == 2
