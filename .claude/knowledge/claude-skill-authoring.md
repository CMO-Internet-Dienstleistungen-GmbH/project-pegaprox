---
category: Claude Code
subcategory: Skills
description: Claude Code skill authoring best practices, structure, patterns, and anti-patterns from official Anthropic guides
file_patterns:
    - '**/skills/**/SKILL.md'
    - '**/.claude/skills/**'
    - '**/commands/**'
last_updated: "2026-04-07"
technologies:
    - claude-code
    - skills
topic: claude-skill-authoring
---
# Claude Skill Authoring Best Practices

## Core Principles

- Every token in a skill competes with conversation history and other injected context. Keep skills terse.
- Only include context that is not already covered by Claude's base knowledge. Skip explanations of well-known concepts (what PDFs are, how libraries work).
- SKILL.md body under 500 lines. Move detailed reference to separate files.
- Description front-loads the most important use case — capped at 250 chars in skill listing.

## Frontmatter

- `name`: kebab-case, max 64 chars, no spaces/capitals/XML tags, no "claude"/"anthropic".
- `description`: WHAT it does + WHEN to use it (trigger phrases). Third person always. Max 1024 chars. No XML tags.
- `disable-model-invocation: true`: Only user can invoke (deploy, commit, destructive ops).
- `user-invocable: false`: Only Claude can invoke (background knowledge, not actionable as command).
- `context: fork` + `agent: <type>`: Run in subagent. Only for skills with explicit task instructions, not reference/guidelines.
- `allowed-tools`: Space-separated or YAML list of tools permitted without approval.
- `paths`: Glob patterns limiting when skill activates (monorepo support).
- `effort`: low/medium/high/max — override session effort level.
- `argument-hint`: Shown in autocomplete (e.g., `[issue-number]`).

## Description Writing

- ALWAYS third person ("Processes X", not "I help you" or "You can use this").
- Include specific trigger phrases users would say.
- Mention file types if relevant.
- Bad: "Helps with projects" / "Processes data" / "Does stuff with files".
- Good: "Extract text and tables from PDF files, fill forms, merge documents. Use when working with PDF files or when the user mentions PDFs, forms, or document extraction."

## Naming

- Gerund form preferred: `processing-pdfs`, `analyzing-spreadsheets`, `testing-code`.
- Acceptable: noun phrases (`pdf-processing`), action-oriented (`process-pdfs`).
- Avoid: vague (`helper`, `utils`), overly generic (`documents`, `data`).

## Degrees of Freedom

- **High** (text instructions): Multiple approaches valid, context-dependent. Code reviews, analysis.
- **Medium** (pseudocode/scripts with params): Preferred pattern exists, some variation acceptable.
- **Low** (exact scripts, no params): Fragile operations, consistency critical. DB migrations, deployments.

## Progressive Disclosure (3 levels)

1. **Frontmatter** (always loaded): name + description for Claude to decide relevance.
2. **SKILL.md body** (loaded when relevant): Full instructions and guidance.
3. **Linked files** (loaded on demand): Reference docs, examples, scripts.

Keep references ONE level deep from SKILL.md. No nested references (SKILL→A→B→actual info).

## Skill Content Types

- **Reference content**: Conventions, patterns, domain knowledge. Runs inline. No `context: fork`.
- **Task content**: Step-by-step instructions for actions. Often `disable-model-invocation: true` + `context: fork`.

## Workflow Pattern

- Break complex ops into clear sequential steps with checklist:
```
Task Progress:
- [ ] Step 1: Analyze
- [ ] Step 2: Plan
- [ ] Step 3: Execute
- [ ] Step 4: Verify
```

## Feedback Loop Pattern

- Run validator → fix errors → repeat. Greatly improves output quality.
- Validation scripts with verbose, specific error messages help Claude fix issues.

## Template Pattern

- Strict: "ALWAYS use this exact template structure" + template.
- Flexible: "Sensible default format, use your best judgment" + template.

## Common Anti-Patterns

- Windows-style paths (`\` instead of `/`).
- Offering too many options — provide a default with escape hatch.
- Time-sensitive information — use "old patterns" section with `<details>`.
- Inconsistent terminology — pick one term, use it throughout.
- Vague descriptions missing trigger phrases.
- Over-explaining what Claude already knows (what PDFs are, how libraries work).
- Deeply nested file references (SKILL→A→B→content).
- SKILL.md over 500 lines without splitting into reference files.

## For Skills with Code

- Scripts should handle errors explicitly, not punt to Claude.
- No "voodoo constants" — document why each value was chosen.
- Pre-made utility scripts > Claude-generated code (more reliable, saves tokens).
- Make clear: "Run script" (execute) vs "See script" (read as reference).
- Plan-validate-execute pattern for complex/destructive operations.
- Use verifiable intermediate outputs (JSON plan → validate → execute).

## String Substitutions

- `$ARGUMENTS` / `$ARGUMENTS[N]` / `$N`: User-provided arguments.
- `${CLAUDE_SESSION_ID}`: Current session ID.
- `${CLAUDE_SKILL_DIR}`: Directory containing SKILL.md.
- `!`command``: Dynamic context injection — shell command runs before prompt sent to Claude.

## Testing Checklist

1. Triggering: loads on obvious tasks, paraphrased requests, NOT on unrelated topics.
2. Functional: correct outputs, API calls succeed, error handling works, edge cases covered.
3. Performance: fewer tool calls, fewer tokens, fewer user corrections vs baseline.

## Quality Checklist

- [ ] Description specific with key terms and trigger phrases (third person)
- [ ] SKILL.md body under 500 lines
- [ ] Additional details in separate files (if needed)
- [ ] No time-sensitive information
- [ ] Consistent terminology throughout
- [ ] Examples concrete, not abstract
- [ ] File references one level deep
- [ ] Progressive disclosure used appropriately
- [ ] Workflows have clear steps with validation
- [ ] Scripts solve problems (not punt to Claude)
- [ ] No magic constants
- [ ] Forward slashes only (no backslash paths)

## Skill Categories

1. **Document/Asset Creation**: Style guides, templates, quality checklists. No external tools needed.
2. **Workflow Automation**: Multi-step processes, validation gates, iterative refinement loops.
3. **MCP Enhancement**: Domain expertise + tool orchestration. Coordinates multiple MCP calls.

## Description Budget

- Total budget: 1% of context window (fallback: 8K chars). Each entry capped at 250 chars.
- Set `SLASH_COMMAND_TOOL_CHAR_BUDGET` env var to increase.
- Front-load the most important use case in description.