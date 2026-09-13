// Loads web/src/*.js into a jsdom window, the same way web/Dev/build.sh loads it
// into a browser: concatenate in dependency order, compile the JSX once, run it.
//
// Only the prefix of build.sh's SRC_FILES up to tables.js is loaded. dashboard.js
// is not needed — the components under test reach the outside through the
// window.PegaProx* registry that ui.js and tables.js already populate.
//
// React comes from static/js/, i.e. the exact bundles the appliance ships, rather
// than an npm copy that could drift from them.
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');
const babel = require('@babel/core');

const REPO = path.resolve(__dirname, '../..');

// build.sh's order, minus the files nothing under test reaches into. Keeping the
// list short matters: this is compiled on every run.
const SRC_FILES = [
    'constants.js', 'translations.js', 'contexts.js', 'auth.js', 'icons.js',
    'ui.js', 'tables.js',
];

function loadApp() {
    const jsx = SRC_FILES
        .map(f => fs.readFileSync(path.join(REPO, 'web/src', f), 'utf8'))
        .join('\n');

    const { code } = babel.transformSync(jsx, {
        presets: [require.resolve('@babel/preset-react')],
        compact: false, babelrc: false, configFile: false,
    });

    const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
        runScripts: 'dangerously', pretendToBeVisual: true, url: 'http://localhost/',
    });
    const { window } = dom;
    const errors = [];
    window.addEventListener('error', e => errors.push(String(e.message)));

    const inject = (src) => {
        const s = window.document.createElement('script');
        s.textContent = src;
        window.document.body.appendChild(s);
    };

    inject(fs.readFileSync(path.join(REPO, 'static/js/react.production.min.js'), 'utf8'));
    inject(fs.readFileSync(path.join(REPO, 'static/js/react-dom.production.min.js'), 'utf8'));

    // The concatenation is not wrapped in build.sh's IIFE here, so the top-level
    // declarations stay reachable — append the handful the tests need.
    inject(code + '\n;window.__test = { LanguageProvider, AuthProvider, useAuth, ClusterHealthBadge };');

    if (errors.length) throw new Error('window errors while loading: ' + errors.join(' | '));
    return { dom, window };
}

// Lets queued microtasks, promise callbacks and 0ms timers run.
const settle = (ms = 0) => new Promise(r => setTimeout(r, ms));

module.exports = { loadApp, settle, SRC_FILES, REPO };
