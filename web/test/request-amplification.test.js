// Guards the invariants behind the request-amplification fix.
//
// The defect: authFetch was a plain per-render arrow function in dashboard.js, and
// getAuthHeaders a plain per-render arrow function in contexts.js. Seven components
// in ui.js carry authFetch in a hook dependency list, so every render re-armed all
// of them — their intervals were cleared and re-fired at once. Because every SSE
// frame renders the dashboard, the push channel ended up driving the polling.
//
// Measured against a stub backend, one idle tab on the overview: 279 /health calls
// in 119 s before, 2 after (the interval is documented as 60 s).
//
// These tests assert behaviour, not source text: render, re-render, count the
// requests. `identity is not preserved` is the sensitivity check — it fails if the
// bug returns AND proves the passing test above it is not trivially green.

const { test, before, after } = require('node:test');
const assert = require('node:assert');
const { loadApp, settle } = require('./harness');

let win, React, ReactDOM, h;

before(() => {
    ({ window: win } = loadApp());
    React = win.React;
    ReactDOM = win.ReactDOM;
    h = React.createElement;
    // jsdom ships no fetch; AuthProvider calls /api/auth/check on mount.
    win.fetch = async () => ({ ok: false, status: 401, json: async () => ({ authenticated: false }) });
});

// The components under test arm setInterval/setTimeout. jsdom timers keep node's
// event loop alive, so every root is unmounted and the window closed at the end —
// without that the run hangs after the last assertion instead of exiting.
const roots = [];

function mount() {
    const el = win.document.createElement('div');
    win.document.body.appendChild(el);
    const root = ReactDOM.createRoot(el);
    roots.push(root);
    return root;
}

after(() => {
    for (const r of roots) { try { r.unmount(); } catch (_) { /* already gone */ } }
    try { win.close(); } catch (_) { /* nothing to close */ }
});

const healthResponse = () => ({
    ok: true,
    json: async () => ({ score: 100, band: 'excellent', factors: [], issues: [] }),
});

test('getAuthHeaders keeps its identity across provider re-renders', async () => {
    // AuthProvider calls useTranslation(), so it needs LanguageProvider around it.
    const { LanguageProvider, AuthProvider, useAuth } = win.__test;
    const seen = [];
    function Probe() {
        seen.push(useAuth().getAuthHeaders);
        return null;
    }

    const root = mount();
    for (let i = 0; i < 5; i++) {
        root.render(h(LanguageProvider, null, h(AuthProvider, null, h(Probe, { n: i }))));
        await settle(20);
    }

    assert.ok(seen.length >= 5, `expected >=5 renders, saw ${seen.length}`);
    const unique = new Set(seen).size;
    assert.strictEqual(unique, 1,
        `getAuthHeaders must keep one identity across renders, saw ${unique}. ` +
        'It is a dependency of authFetch, which every polling effect in ui.js depends on.');
});

test('ClusterHealthBadge fetches once across many re-renders when authFetch is stable',
    async () => {
        const { ClusterHealthBadge } = win.__test;
        const calls = [];
        const authFetch = async (url) => { calls.push(url); return healthResponse(); };

        const root = mount();
        for (let i = 0; i < 10; i++) {
            root.render(h(ClusterHealthBadge, { clusterId: 'c1', authFetch, apiUrl: '/api' }));
            await settle();
        }

        assert.strictEqual(calls.length, 1,
            `expected 1 request across 10 renders, got ${calls.length}. ` +
            'The 60s interval must not be re-armed by rendering.');
    });

test('sensitivity check: an unstable authFetch identity is what caused the storm',
    async () => {
        const { ClusterHealthBadge } = win.__test;
        const calls = [];

        const root = mount();
        for (let i = 0; i < 10; i++) {
            // a NEW function per render — exactly what dashboard.js used to hand down
            const authFetch = async (url) => { calls.push(url); return healthResponse(); };
            root.render(h(ClusterHealthBadge, { clusterId: 'c1', authFetch, apiUrl: '/api' }));
            await settle();
        }

        // Not an exact count: React may batch, and the point is the order of
        // magnitude, not the number. One request is the fixed behaviour; many is
        // the defect this whole change exists to remove.
        assert.ok(calls.length >= 5,
            `expected many requests across 10 renders, got ${calls.length} — if this ` +
            'drops to 1, the test above no longer proves anything and needs rewriting.');
    });

test('IP cache: a "no address" answer counts as an answer, not as a miss', () => {
    const c = win.PegaProxIpCache;
    const [cid, vmid] = ['cache-test-a', 900];

    assert.strictEqual(c.entry(cid, vmid), undefined, 'unknown guest must be fetched');

    c.map.set(c.key(cid, vmid), { ip: null, at: Date.now() });
    assert.notStrictEqual(c.entry(cid, vmid), undefined,
        'a guest with no agent must not be re-requested — leaving this falsy is what ' +
        'closed the fetch/ipTick/re-render loop');
    assert.strictEqual(c.value(cid, vmid), '', 'no address renders as blank');
});

test('IP cache: entries are separated per cluster', () => {
    const c = win.PegaProxIpCache;
    c.map.set(c.key('cache-test-b1', 100), { ip: 'addr-alpha', at: Date.now() });

    assert.strictEqual(c.value('cache-test-b1', 100), 'addr-alpha');
    assert.strictEqual(c.value('cache-test-b2', 100), '',
        'the cache outlives the cluster selection, so vmid alone would collide');
    assert.notStrictEqual(c.key('cache-test-b1', 100), c.key('cache-test-b2', 100));
});

test('IP cache: entries expire, so a changed address is picked up', () => {
    const c = win.PegaProxIpCache;
    const [cid, vmid] = ['cache-test-c', 100];

    c.map.set(c.key(cid, vmid), { ip: 'addr-beta', at: Date.now() });
    assert.notStrictEqual(c.entry(cid, vmid), undefined, 'fresh entry is used as-is');

    c.map.set(c.key(cid, vmid), { ip: 'addr-beta', at: Date.now() - 6 * 60 * 1000 });
    assert.strictEqual(c.entry(cid, vmid), undefined, 'stale entry must be refetched');
    assert.strictEqual(c.value(cid, vmid), 'addr-beta',
        'while refetching, the row keeps showing the last known address');
});
