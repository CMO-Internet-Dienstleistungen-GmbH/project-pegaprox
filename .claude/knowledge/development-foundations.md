---
category: Foundations
description: Universal code quality, architecture (SOLID), error handling, and logging standards — language-agnostic
file_patterns:
    - '**/*'
keywords:
    - development
    - coding
    - architecture
    - solid
    - clean-code
    - naming
    - error-handling
    - logging
    - quality
last_updated: "2026-09-07"
technologies:
    - development
    - coding
    - architecture
topic: development-foundations
---
# Development Foundations

## Naming

- Descriptive, pronounceable names. No abbreviations unless universally understood.
- Booleans: `is`/`has`/`can` prefix. Functions: verb-based. Classes/Types: noun-based. Constants: `SCREAMING_SNAKE_CASE`. Files: `kebab-case`.

## Structure

- One responsibility per function, class, module (SRP). Organize by feature/domain — never by technical layer.
- No magic numbers/strings — extract to named constants. DRY but no premature abstraction (YAGNI).
- Boy Scout Rule: leave every file cleaner than found.

## Never in Code

- Commented-out code, orphan TODOs, `console.log`/debug output, unused imports/vars, secrets/credentials.

## Architecture

- SOLID. Composition over inheritance. Dependency Injection. Separation of Concerns (UI / business logic / data access distinct).
- Business logic independent of framework. Module boundaries explicit. High cohesion within, minimal coupling between.

## Error Handling

- Fail fast. Never swallow exceptions silently. Handle at correct layer. Specific error types, never catch-all.
- Error messages include enough context to diagnose. User-facing errors never expose internals/stack traces.
- Clean up resources in `finally` (or equivalent).

## Logging

- Levels: `ERROR`, `WARN`, `INFO`, `DEBUG`. Log errors with context (timestamp, operation, anonymized user). Include stack traces.
- Never log sensitive data (passwords, tokens, PII). Structured logging where supported. Debug logs only in dev.
- Never change log statements in source to debug — use log level configuration instead.

## Process

- A Python script invoked via `subprocess` always reads the current file from disk, not the in-memory version; a batch process started before an edit will use the new code, not the old one.

## Promoted (2026-08-15)

- [DECISION:0.85] Scanner preserves user-set tier, status, notes, and selfSpeaker fields on re-scan; only auto-derived fields (message_count, date ranges, platform) are overwritten. Manual classifications are protected by separating auto vs. user fields explicitly in the upsert payload.
- [PATTERN:0.90] Use EXISTS subqueries instead of LEFT JOIN when filtering on a many-to-many relation to avoid duplicate rows in the result set without needing GROUP BY or DISTINCT on the outer query.
- [GOTCHA:0.90] Calling a destructive POST endpoint (bulk-cancel, bulk-delete) to 'just test the HTTP status' causes immediate real side effects. For destructive endpoints, verify only via GET render checks or explicit user permission — never fire the mutation as a smoke test.
- [GOTCHA:0.85] SQLite: matching against a JSON column (e.g. aliases) via lower(col) LIKE '%term%' is a substring match on the serialized JSON text — pragmatic but imprecise. Use json_each() for entry-level granularity.
- [GOTCHA:0.90] SQLite UNIQUE constraints ignore NULL values, so multiple rows with the same (person_a, person_b, source) but NULL relation_subtype are all permitted — only non-NULL values are deduplicated.
- [PATTERN:0.87] Split drop counters in LLM extraction pipelines by cause (droppedHallucinated vs droppedLowConf vs droppedSpeaker vs droppedGeneric) and surface them in the job result JSON so you can diagnose which filter is responsible for low recall without guessing.
- [GOTCHA:0.90] In Swift 6 strict concurrency, calling a `self` method inside an `inout` closure that also captures `self` causes an exclusivity violation — extract the method as a static function taking only the affected value.
- [GOTCHA:0.85] Actor reentrancy across `await` suspension points allows multiple concurrent callers to each enter a re-authorization flow simultaneously, even inside a single actor/coroutine context.
- [GOTCHA:0.90] Unterminated string literals caused by curly/typographic quote characters (e.g. ASCII `"` instead of `“`/`”`) make all class members unresolvable — check files byte-by-byte when an entire class appears out of scope.
- [GOTCHA:0.95] In Swift, `deinit` must not access MainActor-isolated properties; use a thread-safe box instead of `nonisolated(unsafe)` to hold task references that need cleanup in deinit.
- [GOTCHA:0.90] Acquiring a semaphore on the main thread while waiting for a MainActor async task causes a guaranteed deadlock; make the shutdown path synchronous and actor-free instead.
- [PREFERENCE:0.75] Validate CI pipeline jobs locally with gitlab-ci-local before pushing to avoid iterative pipeline failures; note that FTP deploy jobs cannot be fully tested locally.
- [PREFERENCE:0.85] Reflection failures that are gracefully handled should be logged at INFO level (message only), not WARN level with stack trace — warn-level is reserved for unexpected, unhandled problems.
- [PATTERN:0.92] Worker job system uses atomic SQLite UPDATE … WHERE status='pending' … RETURNING * claim pattern under WAL mode to safely support a single-worker polling loop without race conditions.
- [PATTERN:0.90] Worker concurrency implemented as a slot-pool with Promise.race(active) to wake the loop when a slot frees; CAS-guarded claimNextJob() prevents race conditions; stop signal drains with Promise.allSettled(active).
- [PATTERN:0.97] Deterministic CI releases: stamp every snapshot image tag with the commit SHA (`<yyyymmdd>-<hhmm>-<shortsha>-SNAPSHOT`). A `resolve` stage at release time walks `git log` to find the most-recent snapshot whose commit is an ancestor of the release tag commit (`git merge-base --is-ancestor`), then fails fast if none exists. This guarantees the release artifact always matches a specific commit without rebuilding.
- [GOTCHA:0.90] When multiple threads download the same URL list, duplicate URLs across subscriptions cause redundant parallel downloads; deduplicate by URL in the main thread before task dispatch to avoid race conditions without locking complexity.
- [CONVENTION:0.80] Export/import of subscription configurations must include all filter fields; omitting them causes silent data loss on backup restore. Always audit all output paths (list, export, import, help, README) when adding new fields to a data model.
- [CONVENTION:0.80] Use FNV-1a hashing instead of Swift's hashValue for deterministic avatar color assignment across app restarts.
- [PATTERN:0.80] Verify parsed financial data against PDF summary blocks (totals, transaction counts) before writing output files — catches off-by-one and header-bleed errors before they reach the user.
- [PATTERN:0.80] When reading git repository state from an agent, keep the reader strictly read-only by design, not by promise — an agent working in a repo is the worst place for unexpected write access.
- [PATTERN:0.80] Apply a per-command timeout to every git subprocess so that repos on hanging network mounts cannot block the calling agent indefinitely.

## Porting scripts into one process: module-level env reads become silent defaults

A collection of standalone scripts run by a shell wrapper reads its settings at
module level without trouble — each process starts *after* the wrapper's
`export` lines:

```python
DAYS = int(os.environ.get("DH_INVOICE_DAYS", "95"))     # fine as a script
OWN  = tuple(os.environ.get("DH_OWN_DOMAINS", "").split(","))
```

Merged into one application, those same lines run at **import**, before any
code has set anything. Nothing raises. Every value is the default, and the
program produces a plausible, wrong result.

Measured (2026-09-07): the invoice matcher lost its own
domains this way and booked eight of the user's own forwarded mails as claims
against himself, plus a stored manual classification reverted to "unpaid". The
comparison against the pre-port baseline did not catch it, because that run
happened while a second defect suppressed the data the variables affected.

Two guards, both cheap and both AST-based rather than convention:

1. **Every variable that is read is also set.** Collect the names appearing in
   `os.environ.get("X")` across the package, and the names appearing in the one
   module that sets them; assert the first set is a subset of the second.
2. **No `os.environ` access at module scope.** Walk the module body, skip
   `FunctionDef`/`AsyncFunctionDef` subtrees, and fail on any remaining
   `os.environ.get(...)` or `os.environ[...]`. Inside a function the same
   access is correct.

The port itself is one line per site: turn the constant into a function
(`def days() -> int: return int(os.environ.get(...))`) and call it at the use
site. Do not "fix" it by setting the variables earlier — the import order of a
package is not something a caller should have to reason about.
