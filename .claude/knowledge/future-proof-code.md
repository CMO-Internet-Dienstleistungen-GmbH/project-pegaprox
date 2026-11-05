---
category: Foundations
description: 'Measurable rules for clean, maintainable, future-proof code: size limits, expression complexity, comments that explain intent, no literal design values, test naming, and the review checklist that enforces them'
file_patterns:
    - '**/*'
keywords:
    - clean-code
    - maintainability
    - readability
    - function-length
    - ternary
    - regex
    - magic-values
    - test-naming
    - review-checklist
    - spaghetti
last_updated: "2026-09-06"
subcategory: Craft
technologies:
    - development
    - coding
    - typescript
    - react
topic: future-proof-code
---
# Future-Proof Code

`development-foundations` says what good code is. This topic says how to recognise it in a diff, with numbers, so a review can say yes or no without taste. The rules come from real review pain (2026-09-06, system-graph: a 759-line component, 1,000-character JSX lines, chained ternaries, hex colours in a canvas, unexplained maths and regexes). Related: `clean-code-development` for the grades these rules serve.

## Size limits

| Unit | Limit | When exceeded |
|---|---|---|
| Line | 120 characters | Break JSX into one element per line; extract the expression into a named const. |
| Function | 40 lines of body, 3 parameters | Extract steps into named functions at one level of abstraction (SLA). |
| React component | 150 lines per file, one component per file | Split into container (data, state) and presentation (markup). Hooks for every effect group. |
| Module | 300 lines | Split by responsibility, not by line count. |
| Nesting | 3 levels | Early return, guard clause, extract. |
| useState in one component | 6 | Reduce with a reducer, RTK slice, or move state down. |

Limits are review thresholds, not lint failures on the first violation: a violation needs a one-line justification in the review or gets fixed.

## Expressions

- No nested ternary. One `? :` per expression at most; anything else is an `if`, a lookup table, or a function with a name.
- No chain of `condition ? a : condition2 ? b : ...` for mapping values. Use a `Record<Key, Value>` or a `switch` with exhaustive check.
- Boolean expressions with more than two operators get a name: `const isVisibleAndSelected = ...`.
- Arithmetic beyond one operator gets either a function whose name says what it computes (`edgeMidpoint`, `zoomAroundCursor`) or a comment stating the formula and why. Layout maths, colour maths and geometry always.
- One statement per line. No `a(); b();` and no `if (x) { y(); z(); }` on one line.

## Comments explain intent

- Every regular expression carries a comment saying what input it accepts and one example of what it rejects. A regex that guards security (allowlist, path check) also states the threat it blocks.
- Every non-obvious constant carries its unit and reason (`FRESHNESS_SECONDS = 86_400 // 24 h, decision 2026-09-05`).
- Every workaround names the bug or limitation it works around and when it can go.
- No comments that repeat the code. No commented-out code.

## No literal design values in code

- Colours, spacing, radii, fonts, durations and z-indexes come from tokens (CSS custom properties or a theme object). A hex literal, a `px` value or a font name outside the token files is a defect. Add a lint step that greps for them.
- Canvas and SVG code reads the same tokens through `getComputedStyle` or a theme object passed in; it never owns a palette.
- Copy lives in one strings module per feature, even if there is only one language today.

## Structure

- Feature folders, not layer folders: `features/<name>/{components,hooks,state,api,tests}`.
- Framework at the edge: routing, HTTP, storage and rendering adapters wrap a framework-free core. Domain code imports nothing from React, Next.js or Node.
- A page is a route. Six views mean six routes with a shared layout, not one page with a view state and a hash.
- State that more than one component reads lives in a store (Redux Toolkit) with typed selectors; server data goes through a query layer (RTK Query) with cache and invalidation rules written down. Secrets never enter either.
- Prefer composition: small components with explicit props over one component with mode flags.

## Duplication

- The third copy of a pattern is the signal to extract, and the extract gets a test.
- Near-identical files (`x-loader.ts`, `y-loader.ts`, `z-loader.ts`) become one generic unit plus small per-case configuration.
- Run a duplication detector (jscpd or equivalent) in the pipeline; fail above 3 % duplicated lines.

## Tests

- Test files live in a `tests/` folder next to the unit they cover, named after the unit: `features/search/tests/search.test.ts`, `ui/Button/tests/Button.test.tsx`.
- Naming states subject and expected behaviour: `describe('Filter Graph')` with `it('Should clear the selection only after its node becomes invisible')`. The rendered name reads as a sentence: "Filter Graph - Should clear the selection only after its node becomes invisible". One behaviour per `it`.
- Assertions use an expressive matcher API (`expect(x).toEqual(...)`, `toContain`, `toHaveLength`, `toThrow`) rather than bare `assert.deepEqual`. Reading the assertion should not require reading the arguments twice.
- Arrange, act, assert, visually separated. No test asserts only the fake it constructed.
- UI components get component-level tests (render, interaction, accessibility role) with a testing library, not snapshot dumps.

## Review checklist

Before approving a diff, confirm each line or note the justification:

1. No unit exceeds the size table.
2. No nested ternary, no mapping chain, no unnamed complex expression.
3. Every regex and every non-trivial formula has an intent comment.
4. No colour, spacing, font or copy literal outside tokens and string modules.
5. New behaviour has a test with a sentence name in the unit's `tests/` folder.
6. No third copy of a pattern without an extract.
7. Framework types stay at the edge; the domain compiles without the framework.
8. Boy Scout Rule applied: the touched file is cleaner than before, and nothing unrelated was touched.
