# Frontend behaviour tests

Guards the invariants behind the request-amplification fix: that `getAuthHeaders`
and `authFetch` keep a stable identity, and that the guest-agent IP cache treats a
"no address" answer as an answer.

## Running

```bash
cd web/test
npm install
npm test
```

`tests/test_ui_request_amplification.py` runs the same thing from the Python
suite, so `python -m pytest` picks it up too — it skips if node or the npm
dependencies are unavailable rather than failing.

## How it works

`harness.js` loads `web/src/*.js` into a jsdom window the way `web/Dev/build.sh`
loads it into a browser: concatenate in dependency order, compile the JSX once,
run it. Only the prefix up to `tables.js` is needed — the components under test
are reachable through the `window.PegaProx*` registry that `ui.js` and
`tables.js` already populate, so `dashboard.js` never has to be loaded.

React comes from `static/js/`, i.e. the exact bundles the appliance ships, rather
than an npm copy that could drift from them. The only dependencies are jsdom and
Babel.

## A note on the sensitivity check

`sensitivity check: an unstable authFetch identity is what caused the storm`
deliberately reproduces the defect. It renders `ClusterHealthBadge` with a fresh
function per render and asserts that this *does* cause repeated requests. If it
ever starts passing for the wrong reason, the test above it no longer proves
anything — a green suite where the guard cannot fail is worse than no guard.
