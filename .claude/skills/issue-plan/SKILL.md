---
name: issue-plan
description: Turns a completed PegaProx triage into an implementation-ready Issue draft and creates it in the CMO GitHub fork after exact user approval. Use after issue triage or when asked to plan a fork Issue.
---

# PegaProx Issue planning

Turn an established request and triage result into a public, durable scope
definition for implementation and future Upstream rebases. Plan only; do not
write product code, create a Branch, or implement the change.

## Establish the evidence

Before drafting:

1. Read the automation repository's `README.md` section *Where an internal
   request goes*, `CLAUDE.md`, and `patches.yml`.
2. Inspect the relevant code in the verified fork checkout or `origin/cmo/main`.
   Identify the current behavior and likely cause from evidence, not only the
   reported symptom.
3. Search open and closed Issues in
   `CMO-Internet-Dienstleistungen-GmbH/project-pegaprox` for duplicates and
   related scope. Read relevant Issue bodies and comments.
4. Check whether Upstream already implements or tracks the request when this
   affects category B/C or the proposed scope.
5. Keep uncertainties explicit. Ask up to three concrete questions when an
   answer changes behavior, scope, acceptance criteria, category, or priority.

Do not turn a rough triage sketch into invented technical detail. A plan is
ready only when it passes INVEST: independently actionable, valuable,
estimable in words, small enough for one coherent Patch, and verifiable. Split
unrelated concerns into separate proposed Issues instead of hiding them in one.

## Draft the Issue

Write in German while retaining domain terms such as Issue, Label, Patch,
Upstream, Release, Fork, Branch, Feature, Bug, Config, session, prompt, and
hook. Match the useful shape of existing Issues such as #7 and #13 without
copying their content.

Use an observable-result title. Prefer `Patch: ...` for a concrete fork Patch
and `Wunsch: ...` for a not-yet-committed Feature direction.

The body contains only sections that add information, normally:

```markdown
<one short paragraph stating whether work exists and what this Issue defines>

## Das Problem

<observable behavior, impact, and relevant measurements>

## Ursache

<code-backed cause, when established; otherwise state what remains unknown>

## Umfang

- <implementation boundary and affected components/files>
- <additive hook/module where possible; unavoidable upstream touchpoints>
- <required generated artifacts, tests, or documentation>

## Akzeptanzkriterien

- [ ] <concrete, falsifiable result at the lowest real verification edge>

## Nicht Teil davon

- <explicitly excluded adjacent work>

## Upstream

<existing Issue/PR or the intended route for category B/C>

<!-- pegaprox-triage -->
```

Category A may instead document the exact Config path and verification. Omit a
speculative `Ursache` section. Do not add hour estimates, implementation code,
or an `Ergebnis` section for work that has not happened.

Select exactly one category Label and one priority Label:

- `triage/A-config`, `triage/B-bug`, `triage/C-feature`, or
  `triage/D-internal`
- `prio/blocker`, `prio/hoch`, `prio/mittel`, or `prio/niedrig`
- Add `triaged` when the plan is complete.

Use `needs-info` instead of `triaged` while questions remain, and
`needs-review` when a human decision is required. Do not apply `completed`,
`duplicate`, `invalid`, or `wontfix` during creation.

## Public-data check

Everything in this repository is public. Before showing the preview and again
before creating the Issue, remove internal hostnames, node/cluster/pool names,
domains, addresses, people, login names, storage targets, tokens, UUIDs, and
other environment identifiers. Preserve product versions, counts, sizes,
measurements, product endpoint paths, runtime versions, and product-defined
values. `127.0.0.1` and the public fork URL are the only permitted addresses.

Run the repository's required pattern check over the finished title and body:

```text
[0-9]{1,3}(\.[0-9]{1,3}){3}|b06|c06|infra-|PegaProx_c[0-9]|cmo\.de
```

Only an intentional `127.0.0.1` match may remain. The regex is a minimum; also
review the prose semantically. Never paste unsanitized logs into a draft.

## Preview and approval gate

Show the user, in this order:

- the implementation plan, including affected components, ordered steps,
  verification, Upstream route, risks, and explicit exclusions;
- the exact Issue title;
- the exact Issue body;
- the exact Labels;
- the fixed target repository:
  `CMO-Internet-Dienstleistungen-GmbH/project-pegaprox`.

Then ask the user to answer with `ok`. This preview is the approval object. A
plain `ok` is valid only as the immediate answer to this current preview. A
change request, new evidence, ambiguity, or a different target invalidates it;
show the complete revised preview and require a new `ok`.

## Create and verify

After valid approval:

1. Repeat the duplicate search and public-data check because GitHub creation is
   an external, public write.
2. Create exactly the approved title, body, and Labels in the fixed target
   repository with an available GitHub tool or `gh issue create`.
3. Read the created Issue back from GitHub and compare repository, title, body,
   and Labels with the preview.
4. Report the Issue number and URL. If creation or readback fails, report the
   observed error and do not claim success or retry with changed content.

Never create an Upstream Issue in this skill. Upstream communication is a
separate workflow with its own live-template and human-publication rules.
