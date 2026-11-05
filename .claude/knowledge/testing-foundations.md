---
category: Foundations
description: Universal testing principles (TDD, Arrange-Act-Assert, mocking) — complements technology-specific testing knowledge
file_patterns:
    - '**/*Test*'
    - '**/*test*'
    - '**/*spec*'
    - '**/*_test.*'
keywords:
    - testing
    - tdd
    - test
    - unit-test
    - integration-test
    - mock
    - assert
    - coverage
last_updated: "2026-09-06"
subcategory: Testing
technologies:
    - testing
    - tdd
topic: testing-foundations
---
# Testing Foundations

These practices apply only after project instructions or the user's current request explicitly opt into testing or TDD for executable application or library source. This topic does not activate testing by itself. Existing tests, task size, or a generic skill are not an opt-in. Documentation, rules, prompts, configuration, data, authored assets, and small script projects without an explicit testing requirement use proportional non-test verification.

- When testing is explicitly required, write tests before implementation when possible (Red → Green → Refactor). No opted-in source-code feature is complete without its prescribed tests.
- Arrange-Act-Assert pattern. One assertion concept per test. Test names describe what is tested and the expected outcome.
- Mock all external dependencies (APIs, databases, file system, time). In-memory/fake adapters for unit tests — no real Redis/DB/S3.
- Do not test framework or library code. No flaky tests — fix or delete. Test edge cases and error paths, not only happy path.

## Promoted (2026-08-15)

- [GOTCHA:0.80] Hardcoded absolute dates in tests (e.g. 2026-08-04 expected as sealed) rot as time passes — use relative time offsets (time.Now().Add) instead of fixed future dates.
- [CONVENTION:0.82] Integration tests for external providers (ElevenLabs synthesize smoke, Gladia stream smoke) use build tag 'integration' and skip cleanly when API keys are absent, keeping CI green without credentials.
- [GOTCHA:0.95] AppleScript `System Events click` does not produce gesture-compatible mouse events in SwiftUI apps. Use real CGEvents (CGEventCreateMouseEvent / CGEventPost) for automated UI testing that triggers SwiftUI gesture recognizers.
- [PATTERN:0.85] Test shell script logic (e.g. argument-extraction functions) by executing the actual generated script via `/bin/sh` from Swift unit tests rather than reimplementing the logic in Swift; this eliminates divergence between the tested and shipped artefact.
- [GOTCHA:0.95] Alert buttons on macOS 26 can be mirrored to the Touch Bar; XCUITest queries like `buttons['Discard'].firstMatch` may hit the Touch Bar copy instead of the window button — always scope queries to `app.windows.firstMatch`.
- [GOTCHA:0.90] SwiftUI `.onTapGesture` does not respond to Accessibility/VoiceOver synthetic clicks in XCUITest; use `Button` or `.contentShape` + proper hit testing for reliable AX interaction.
- [PATTERN:0.90] Inject all filesystem roots (SSH dir, agent.toml path, fake-op binary) via environment variables (SCM_SSH_DIR, SCM_AGENT_TOML, SCM_FAKE_OP) so tests and development always run against fixtures, never the real ~/.ssh.
- [GOTCHA:0.85] XCUITest `.label` returns the full untruncated string even when a button label is visually clipped — visual truncation bugs (e.g. 'Cancel' rendered as 'Can…') cannot be detected by label-based assertions and require screenshot review or frame-size checks.
- [PATTERN:0.85] Verify subprocess-heavy shell integration end-to-end using fake op/ssh binaries (exit 0 + canned stdout) rather than live biometric-gated tools — keeps CI headless and deterministic.
- [GOTCHA:0.85] Parallel test agents that each invoke `swift test` can deadlock on SwiftPM's build lock; the 'Another instance is already using the build directory' error may be buffered and invisible, making the hang look like a test hang.
- [DECISION:0.90] For E2E testing React Native flows that leave the app (system file picker via SAF, share sheet), use Maestro over Detox — Detox only controls the app process and cannot reach system UI, while Maestro operates at UIAutomator level and can.
- [GOTCHA:0.90] API 28 is the last Android version where WRITE_EXTERNAL_STORAGE permission has any effect; use it as the primary test target before testing lower API levels.
- [GOTCHA:0.85] The 'runmaestro' Homebrew cask installs the wrong Maestro tool; the correct one for mobile testing is the 'maestro' formula from mobile.dev at /opt/homebrew/bin/maestro.
- [GOTCHA:0.95] When using Maestro (or similar UI test tools), a second 'input text' command appends to existing field content instead of replacing it — always clear the field first or use a dedicated clear+type action to avoid mixed/corrupted input values.
- [GOTCHA:0.85] A test assertion can appear green for the wrong reason: an empty field looks the same as a successfully cleared field — always verify the field actually received and persisted the value, not just that it is empty.
- [GOTCHA:0.95] On Android, files placed via `adb push` are registered in the MediaStore as `application/octet-stream` instead of their actual MIME type (e.g. `text/csv`), causing them to be filtered out by MIME-type pickers — this is a test-setup artifact, not a permission problem.
- [CONVENTION:0.75] UI regression tests must reproduce the exact failing interaction path (e.g. type code → tap button without closing keyboard) so the test fails on a revert and passes only after the real fix.
- [GOTCHA:0.85] When targeting tab bar items by label in Maestro, a label that also appears in a data section (e.g. 'Entries') is ambiguous and hits the wrong element; use positional selectors for tab bars instead.
- [DECISION:0.90] Android SAF file picker flows are intentionally not automated in Maestro because picker UI varies by Android version and file manager, making such tests flaky; manual steps are documented in README instead.
- [CONVENTION:0.85] Maestro subflows placed in a subflows/ directory are not executed separately when running 'maestro test .maestro/' — only top-level flow files are picked up.
- [CONVENTION:0.90] Use consistent testID naming scheme for navigation elements: tab-<name>, fab-<action>, <screen>-<element>-button, e.g. tab-entries, fab-add-entry, entry-save-button.
- [GOTCHA:0.90] XCUITest cannot inspect the content of a remote view controller rendered by a Share Extension host; only the navigation bar is accessible, so UI tests for transcript content inside an extension will always time out.
- [GOTCHA:0.80] SwiftUI `.accessibilityIdentifier` on a container view propagates to child elements, not to an 'Other' accessibility element on the container itself; place identifiers directly on Text or Button elements for reliable XCUITest queries.
- [GOTCHA:0.85] A unique identifier (e.g. test ID or DOM id) placed on a row container silently overwrites/shadows the same identifier on a nested element (e.g. a download button) inside that container, causing selectors to match the wrong element.
- [GOTCHA:0.90] UI tests using hardcoded button labels (e.g. 'Delete') fail on localized devices where the label differs (e.g. 'Löschen' in German); use accessibility identifiers or localization-aware queries instead.
- [GOTCHA:0.95] When multiple simulators share the same name (e.g. 'iPhone 17 Pro'), `-destination 'name=…'` silently picks the wrong one. Address simulators by UDID instead.
- [PATTERN:0.75] Verify MCP server correctness with two scenarios: (A) fresh connection from scratch (initialize → tools/list → sub-tool call) and (B) server restart mid-session (sub-tool before restart → restart → sub-tool without reload → loader → sub-tool after reload), both with zero session errors.
- [GOTCHA:0.95] On macOS 26, SwiftUI .alert buttons are not visible to XCUITest, but NSAlert-based sheets are — use NSAlert for testable confirmation dialogs.
- [CONVENTION:0.85] Always verify a new test actually fails when the condition it guards against is introduced (e.g. inject a deliberate typo); a test that cannot fail provides no value.
- [GOTCHA:0.78] When a test suite runs multiple independent checks, a cascading exception from one check can prevent subsequent checks from reporting independently — make each check robust against prior failures so all failures surface in a single run.
- [GOTCHA:0.90] Local commits that bypass CI (pushed only after a merge) may hide test failures that only manifest on slower CI runners due to timing differences. Run E2E with CPU throttling locally to catch race conditions before pushing.
- [PATTERN:0.85] For MCP stdio server integration tests, use both an in-memory test (tool semantics) and a real subprocess test (spawns the compiled binary, sends JSON-RPC over stdin/stdout, asserts clean exit on closed stdin).
- [GOTCHA:0.90] Git clone processes inherit the host's Git config, including 1Password commit signing, which causes test failures when tests run real git operations without an isolated config.
- [PATTERN:0.80] Test git integration against real repositories in a temp directory, not against hand-written or mocked git output, to catch format changes and edge cases reliably.
- [GOTCHA:0.95] Contract tests that start real API sessions on every `swift test` run will silently consume API quota at scale (54 sessions observed). Mock or stub external sessions in test code.
- [GOTCHA:0.95] `defer { Task { … } }` does not complete before a Swift test ends; use `await` for explicit async cleanup instead.
- [GOTCHA:0.85] A process may still write its transcript after the test deletes it; wait for the process to fully terminate before asserting on filesystem state.
- [PATTERN:0.75] When UI test queries fail, dump the actual element/accessibility tree from the running app instead of guessing the structure.
- [GOTCHA:0.95] On macOS, a Switch control reports its value as a number (e.g. 0/1), not as the strings "0"/"1". Casting to String gives nil, which reads as "off" regardless of actual state.
- [GOTCHA:0.95] Waiting for an Outline view to exist is not the same as waiting for its list contents to load — the Outline renders immediately when the sidebar draws, but real data (e.g. 300 sessions) arrives 2–4 seconds later. Tests that check list state without a content-ready condition will silently pass on an empty list.
- [GOTCHA:0.92] A Toggle inside a Form renders as a Switch on macOS, but the same Toggle rendered standalone in a Sheet becomes a Checkbox. UI test queries targeting only the Switch type will miss the Checkbox variant and incorrectly report the control as absent.

## Test naming, layout and assertions (2026-09-06)

- Test files live in a `tests/` folder next to the unit, named after it (`features/search/tests/search.test.ts`, `ui/Button/tests/Button.test.tsx`), not as `<unit>.test.ts` beside the source.
- Names state subject and expectation: `describe('Button')` with `it('Should render a button with the class of primary')`. The rendered "Button - Should render …" sentence must make sense without opening the file. One behaviour per `it`; a name with "and" is two tests.
- Prefer an expressive matcher API (`expect(result.ids).toEqual(['a'])`, `toContain`, `toHaveLength`, `toThrow`) over bare `assert.deepEqual(actual, expected)`; the assertion reads as a sentence and the failure message names the matcher.
- Decided for TypeScript projects: Vitest with Testing Library for components; `node:test` only where a runtime constraint forces it, and then still with `describe`/`it` naming.
