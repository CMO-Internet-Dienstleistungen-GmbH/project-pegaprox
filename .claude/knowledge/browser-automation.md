---
category: Tools
description: 'Browser automation gotchas: HAR capture, fetch interception, Cloudflare bypass'
file_patterns:
    - '**/*.har'
keywords:
    - browser
    - chrome
    - devtools
    - har
    - fetch
    - cloudflare
    - automation
    - scraping
last_updated: "2026-08-15"
subcategory: Browser
technologies:
    - chrome
    - devtools
    - cloudflare
    - har
topic: browser-automation
---
# Browser Automation

## HAR Capture

- HAR exports often capture only initial page load, not subsequent API calls (SSE, polling, WebSocket).
- Auth cookies (HttpOnly, SameSite) frequently stripped from HAR — file lacks authentication context.
- Response bodies may be zero-length if compressed (`gzip`, `br`) and not decoded before export.
- For API reverse-engineering: enable "Preserve log", record full interaction cycle, ensure response bodies are decoded.

## Fetch Interception

- Monkey-patching `window.fetch` after page load often fails — app caches original `fetch` reference during bundle init.
- DevTools MCP `navigate_page` supports `initScript` — JS executed before any other scripts on next navigation. Use this to inject fetch interceptors before the app loads.
- `claude-in-chrome` extension does NOT support `initScript`; its `read_network_requests` captures URL, method, status (no bodies).
- To capture response bodies via `claude-in-chrome`: inject interceptor via `javascript_tool`, then trigger actions (don't reload — interceptor lost on navigation).

## Cloudflare Bot Protection

- Cloudflare blocks all automated Chrome: headless, remote debugging (port 9222), DevTools Protocol.
- `claude-in-chrome` bypasses Cloudflare — runs inside user's real browser session with `cf_clearance` cookies.
- Chrome DevTools MCP connects via remote debugging → blocked by Cloudflare.
- Use `claude-in-chrome` for Cloudflare-protected sites; DevTools MCP for local/unprotected sites.

## Promoted (2026-08-15)

- [PATTERN:0.90] Persistent browser sessions in Selenium/geckodriver can be preserved across tool calls by tracking a dedicated session tab handle and URL/title/snapshot state on the driver object, rather than closing all tabs after each fetch.
- [PATTERN:0.90] Inject data-onion-id attributes (L1, B1, I1, F1) via JavaScript on every page load so that interactive elements can be reliably referenced by index across subsequent Selenium actions (click, type, submit).
- [PATTERN:0.90] Structured page snapshot format for LLM-driven browsing: URL, title, extracted text, numbered links [Ln], forms [Fn] with inputs [In], and buttons [Bn] — enables stateless element addressing across tool calls.
- [PATTERN:0.85] For reverse-engineering undocumented API pagination, use a curl-template approach: capture the exact browser curl command, parameterize only the cursor/ID variable, and replay it. This ensures all authentication headers (`fb_dtsg`, `lsd`, `x-asbd-id`, etc.) are passed correctly without having to reverse-engineer each header.
- [GOTCHA:0.85] Full-page screenshots become too narrow when page height exceeds ~30 000 px; capture only the relevant viewport section instead.
- [PATTERN:0.80] Inject numbered annotation markers directly into the DOM via JS before taking a screenshot to produce self-documenting UI audit images without external tools.
- [PATTERN:0.85] Atomic download pattern: write to *.part file, rename to final name only on success; re-runs resume from existing .part file via HTTP Range requests.
- [GOTCHA:0.80] Chrome DevTools MCP tab state can silently crash (tab shows about:blank) while the MCP connection remains open; screenshot and navigation calls time out without error. Opening a fresh tab in the same browser window restores functionality.
- [GOTCHA:0.90] Playwright's devices['Desktop Chrome'] viewport (1280×720) overrides any global viewport setting in playwright.config.ts; elements positioned relative to a larger viewport become off-screen, causing drags and clicks to fail unexpectedly.
- [PATTERN:0.85] Set playwright retries: 0 in config to prevent flaky tests from being masked by automatic retries; a test that only passes on retry is a hidden reliability problem.
- [GOTCHA:0.82] Parallel fire-and-forget PATCH requests (no await) mean reading the database immediately after triggering them yields stale data. In tests, use polling assertions rather than a single snapshot to verify persistence after optimistic UI updates.
- [GOTCHA:0.75] In Tailwind/React, an absolute-positioned opacity-0 button that becomes visible on group-hover still intercepts pointer events when Playwright hovers to perform a click — the hover itself triggers group-hover and re-enables the overlay. Move overlapping interactive elements out of absolute stacking to avoid this.

