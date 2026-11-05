---
category: Claude Code
description: Claude Code platform behavior, hooks, permissions, MCP registration, and session management
file_patterns:
    - '**/.claude/**'
    - '**/CLAUDE.md'
    - '**/.claude.json'
keywords:
    - claude
    - claude-code
    - hooks
    - mcp
    - permissions
    - settings
    - rules
    - transcript
    - remote-control
last_updated: "2026-09-30"
subcategory: Internals
technologies:
    - claude-code
topic: claude-code-internals
---
# Claude Code Internals

## MCP & Permissions

- MCP servers MUST be registered in `~/.claude.json` under `mcpServers`, NOT in `~/.claude/settings.json`. Using `settings.json` → silent failure (server starts but never appears under `/mcp`).
- Permission matching evaluates the full command string from the beginning. For piped commands, only the first command needs a matching entry.
- `.claude/settings.local.json` with `"defaultMode": "bypassPermissions"` enables zero-prompt for trusted local projects. NOT version-controlled (gitignored).

## Permissions & Allowlist Patterns

**Granular over wildcard.** Prefer narrow verb lists over broad tool-level wildcards. For `kubectl`, enumerate read verbs explicitly (`get`, `describe`, `logs`, `top`, `explain`, `diff`, `config`, `version`, `cluster-info`); keep destructive operations behind a scoped entry like `KUBECONFIG=*test* kubectl *` so production writes still require approval.

**Explicit MCP tools over namespace wildcards.** List individual safe MCP tool names (`mcp__knowledge__search_knowledge`, `mcp__knowledge__get_knowledge`, …) rather than `mcp__knowledge__*`. Namespace wildcards silently authorize any destructive tool that ships later with the same server (`upsert_knowledge`, `delete_knowledge`, `reindex`).

**Safe to auto-allow.** Idempotent read-only operations: `ls`, `cat`, `grep`, `find`, `head`, `tail`, `tree`, `echo`, `cd`, `git diff|log|status`. MCP read-only tools qualify by the same principle.

**Must require approval every time.** Anything that mutates local state or executes arbitrary code against an external surface: `kubectl exec`, `git add`, `rm`, `chmod`, `mkdir`, `go build`, `go test`, knowledge-MCP writes (`append_knowledge`, `upsert_knowledge`, `subscribe`, `reindex`), `gh api`. High occurrence count is not evidence of safety — a frequently-used command that executes remote code (e.g. `gh api`) must still be gated.

**Threshold for promoting a command into the allowlist.** Require at least three independent occurrences across distinct sessions before considering auto-allow, so one-off commands don't enter permanent trust.

**Periodic audit.** One-off allow entries accumulate in `permissions.allow` over time — prune regularly, otherwise the allow-list drifts into an implicit blanket-trust for anything ever added in a moment of haste.

## Hooks

- `PreToolUse` hook matcher must be at top level of hook entry (`"matcher": "Bash(git commit*)"`), NOT inside `"if"`. Wrong placement fires on every tool use.
- `SessionStart` without `"matcher": "startup"` fires on every start AND resume (compaction, /clear). Add `"matcher": "startup"` for new sessions only.
- `/model` switch is treated as new session start → triggers `startup` matcher. Expected behavior.

## Hook-Output Fields (verified against claude-code-source 2.1.x)

- **`hookSpecificOutput.additionalContext`** (PreToolUse, UserPromptSubmit, SessionStart, Setup, SubagentStart, PostToolUse, PostToolUseFailure, Notification) — wrapped in `<system-reminder>` tags and appended as a UserMessage with `isMeta: true`. **Persistent in the transcript until auto-compact**, not ephemeral per-turn. Truncated hard at **10,000 chars** (`MAX_HOOK_OUTPUT_LENGTH` in `processUserInput.ts`).
- **`systemMessage`** (top-level, all hooks) — displayed to the user in the UI as a separate hook message. Use this for transparency banners ("knowledge X injected"). Does NOT go to the model as context.
- **`hookSpecificOutput.updatedInput`** (PreToolUse) — replaces tool args before execution; persistent in transcript.
- **`hookSpecificOutput.updatedMCPToolOutput`** (PostToolUse) — replaces MCP tool result; persistent in transcript.
- **UserPromptSubmit has no persistent replace-field** — only `additionalContext` (+ `systemMessage` for UI).

## additionalContext Lifecycle

- Generated in hook → converted to `hook_additional_context` attachment → inlined as a UserMessage with `<system-reminder>` wrapper.
- **No N-turn redaction for UserMessages.** Previously suspected 5-turn rule does NOT exist — `additionalContext` is a UserMessage with `isMeta: true` and part of normal message history.
- Removed only at auto-compact: `getMessagesAfterCompactBoundary()` drops all messages before the boundary, including past `isMeta` injections. Auto-compact kicks in near 80% of the token budget.
- Practical implication: `additionalContext` is effectively persistent for the scope of a coherent task (pre-compact), NOT per-turn. If you need guaranteed fresh injection, re-inject each turn.

## Microcompact (≠ Auto-Compact)

Runs before auto-compact in `query.ts`. Scope and triggers are narrow — NOT a general context prune.

- **Scope:** Only replaces `tool_result` blocks for `COMPACTABLE_TOOLS` (Read, Edit, Write, Grep, Glob, Bash/Shell, WebSearch, WebFetch) with the literal string `[Old tool result content cleared]`. Other tool results (MCP tools, Task agent, etc.) are untouched.
- **Does NOT touch UserMessages with `isMeta: true`** — `additionalContext` injections survive microcompact. They die only at full auto-compact.
- **Time-based trigger** (`maybeTimeBasedMicrocompact`): when gap since last assistant message > `gapThresholdMinutes` (GrowthBook config), assume the prompt cache is cold anyway and clear old tool_results eagerly. Keep the last `config.keepRecent` tool_results untouched (floor 1).
- **Cached microcompact** (Ant-only, `CACHED_MICROCOMPACT` feature flag): count-based keep-recent on main-thread conversations. Uses the Cache-Editing API to remove tool_results *without* invalidating the prompt cache.
- **Only main-thread `querySource`** — sub-agents (`session_memory`, `prompt_suggestion`) are skipped so they don't corrupt the main thread's cachedMCState.
- **Composes orthogonally with snip + content-replacement**: microcompact only references `tool_use_id`, never inspects content, so earlier replacers and later snippers compose cleanly.
- Metric: `tengu_time_based_microcompact` / `tengu_cached_microcompact` log toolsCleared, tokensSaved, keepRecent.

Implication: if a Read/Grep result "disappears" mid-session but the surrounding text is intact, that's microcompact, not auto-compact. Re-run the tool to get the content back.

## Rules System

- `~/.claude/rules/*.md` always loaded for every project. `paths:` frontmatter is IGNORED at global level (Bug #21858).
- `paths:` frontmatter only works in project-level `.claude/rules/` directories.
- `paths:`-scoped rules only trigger on Read, not on Write (Issue #23478).
- CLAUDE.md rules are soft instructions — system features (like Auto-Memory) require `settings.json` to hard-disable.

## Sub-Agents & Context

- Sub-agents in skill subdirectories are NOT auto-discovered. Require symlink in `~/.claude/agents/`.
- 200K bytes ≈ 50-67K tokens (not 200K tokens). Ratio: roughly 3-4 characters per token.
- Session-start scripts listing all skills are redundant — Claude Code already loads skill metadata via system reminders.

## JSONL Transcript Analysis

- Transcripts at `~/.claude/projects/<encoded-path>/`. Encoding: strip leading `/`, replace all `/` with `-`.
- `"type": "user"` entries: ~85% tool-results, ~15% real typed messages. Real messages: `content` is a string OR list with at least one `"type": "text"` element (not `"tool_result"`).
- Duration: sum active block durations, not clock-span. Gaps >20min are pauses — subtract. Night activity (before 05:00) → attribute to preceding calendar day.

## Sub-Agent Model Selection

- Haiku fails on >20KB unstructured context — ignores tool restrictions, writes ad-hoc Python to `/tmp/`. Fundamental capability limit, not a prompt issue.
- Context-heavy classification (>17KB input): use Sonnet medium effort, or pre-filter in Python to <20KB.
- Pre-filtering data to keywords/summaries is more cost-effective than upgrading the model.

## Knowledge MCP Compliance

- Most effective pattern: place Knowledge MCP check as explicit numbered step in skill/workflow instructions ("Step 0: search_knowledge"), not buried in CLAUDE.md.

## Remote Control API

- Lives at `api.anthropic.com`, NOT `claude.ai` (Cloudflare blocks non-browser clients).
- Required headers: `anthropic-beta: ccr-byoc-2025-07-29`, `anthropic-version: 2023-06-01`.
- Auth: OAuth Bearer token (`sk-ant-oat01-...`). API keys (`sk-ant-api03-...`) do NOT work for remote control.
- OAuth token: macOS Keychain, service name `Claude Code-credentials`, JSON format `{"claudeAiOauth":{"accessToken":"..."}}`.
- Session ID prefixes: `session_XXX` for `/v1/sessions/` endpoints, `cse_XXX` for `/v1/code/sessions/` endpoints.
- Key endpoints: `GET /v1/sessions` (list), `GET /v1/sessions/{id}/events?limit=N&after_id=UUID` (poll), `POST /v1/sessions/{id}/events` (send).
- Cursor-based pagination: `after_id` + `has_more` + `last_id`.
- WebSocket: `wss://api.anthropic.com/v1/sessions/ws/{id}/subscribe?organization_uuid={org}` with 30s ping interval.

## Project CLAUDE.md Template

```
## Project Overview
One-sentence description. Tech stack summary.

## Rules & Constraints
All project-specific rules that MUST always be followed.

## Documentation
- `docs/architecture.md` — system architecture
- `docs/api.md` — API contracts and endpoints
- `docs/decisions/` — architecture decision records

## Current State
- what's done, what's in progress (2–5 bullets)

## Next Steps
- prioritized list of what to do next

## Known Issues
- bugs, blockers, open questions
```

## Hook Output Format

- Hooks must return JSON with `additionalContext` to inject content into Claude's context. Plain text stdout is **silently discarded** — no error, no warning.
- Required format: `{"additionalContext": "content to inject"}` or `{"hookSpecificOutput": {"hookEventName": "EventType", "additionalContext": "..."}}`
- Async hooks (e.g., `PostToolUse` with `"async": true`) cannot inject `additionalContext` — Claude has already moved on. Only synchronous hooks can push context.
- Large CLAUDE.md (~21KB) causes attention degradation on early instructions. Keep behavioral rules terse (~4KB); move coding standards to Knowledge MCP files.

## Session Mining Pipeline

- Gemma4 E2B (2.3B) with thinking enabled consumes all `num_predict` tokens on internal reasoning before generating content, leaving the `response`/`content` field empty; always check for empty content and fall back to the `thinking` field.
- Small 2.3B models lose system-prompt instruction-following when user content is very long; Gemma4 E2B responded in German with a prose summary instead of JSON when the transcript exceeded ~2.5K words.
- Self-referential transcripts (content about the session miner itself) systematically confuse small models into summarizing the text rather than extracting structured insights; Haiku/Sonnet are not affected.
- Dropped Gemma4/Ollama from the session-mining pipeline in favour of a two-tier approach: apfel (Apple Intelligence, ≤1100 words, ~5s, free, offline) → Sonnet low fallback (>1100 words, ~20s).
- An existing session marker without an `engine` field causes `is_already_mined` to skip re-processing even when the session has grown, because size comparison alone gates the check; markers from legacy runs block new engines from running.

## GitLab MCP

- GitLab MCP has no `create_project` tool; project creation requires direct API calls or CLI.

## Anthropic SDK / API

- When using the Anthropic SDK with OAuth, putting the billing header and identity text in the same TextBlockParam causes the model to ignore the entire system prompt. Billing header and identity must be separate system blocks, mirroring Claude Code's splitSysPromptPrefix() approach.
- System prompt split into separate TextBlockParam blocks (billing header first, identity/tools second) to prevent system prompt override suppression observed with claude-sonnet-4-6 via OAuth.
- claude-sonnet-4-6 via OAuth requires the short model ID ('claude-sonnet-4-6'), not the dated long form ('claude-sonnet-4-6-20250514'); the API returns model-not-found for the long form.
- Anthropic prompt caching requires ~1024+ tokens to activate; short system prompts (~50 tokens) produce zero cache_read_input_tokens even with cache_control set correctly.
- OAuth 429 responses on the non-beta Anthropic endpoint mean the beta headers are missing or malformed, not a real rate limit. Betas must be sent as separate comma-separated values, not space-separated in a single header.

## MCP Server Development

- Use Streamable HTTP transport (server.NewStreamableHTTPServer()) instead of stdio for MCP servers; registered in Claude Code via 'type: streamable-http' and 'url' field instead of 'command'.
- The MCP Inspector runs as a browser-based web app and triggers CORS errors when connecting directly to an MCP server on a different port; use the Inspector's built-in proxy mode to avoid needing CORS headers on the server.

## Promoted (2026-04-19)

- [GOTCHA] Cache TTL '1h' (ephemeral_1h) acceptance depends on subscription status and GrowthBook allowlist — must be verified at runtime via usage response; client sets TTL but server may downgrade it.
- [GOTCHA] Required HTTP headers for OAuth-based Claude API calls: `x-app: cli`, `User-Agent: claude-cli/<version> (external, cli)`, plus billing header with version and fingerprint; missing any causes auth failure.
- [GOTCHA] `getMemoryFiles()` in Claude Code is memoized per session — `rules/*.md` files are read once at startup and cached in memory; changes on disk during a session are ignored until the next compact or session restart.
- [GOTCHA] SessionStart hooks fire before `getMemoryFiles()` reads `rules/*.md`, so a SessionStart hook can write a rules file that gets loaded into the system prompt; `InstructionsLoaded` fires after and can clean up the file.
- [GOTCHA:0.95] Claude Code `permissions.allow` wildcards (e.g. `Bash(*kubectl*)`, `mcp__knowledge__*`) completely bypass the autoMode classifier, making any `autoMode.soft_deny` rules ineffective for those tools.
- [GOTCHA:0.90] A failing PreToolUse hook takes precedence over `permissions.allow` evaluation; the hook error triggers a user prompt before allow-list matching occurs.
- [GOTCHA:0.80] Bun can be used as a drop-in runtime for the Claude Code CLI (`bun /path/to/cli.js`) saving ~30–50 MB baseline RAM, but mid-session memory peaks come from conversation heap and MCP buffers, not the runtime.

## Peer Messaging Channel (verified 2026-08-09, CC 2.1.226)

The mechanism behind `SendMessage`/`ListAgents` between local sessions. Stable enough to build on; covers **interactive AND background** sessions (the bg daemon socket covers only bg).

- **Discovery:** `~/.claude/sessions/<pid>.json`, written per running session, removed on clean exit. Crashes leave stale files — trust an entry only if `kill(pid,0)` succeeds. Key fields: `messagingSocketPath`, `peerProtocol` (revision, currently `1`), `sessionId`, `kind` (interactive|bg|daemon|daemon-worker), `status` (idle|busy|shell|waiting), `waitingFor`, `state` (bg terminal outcome), `detail`, `jobId`.
- **`state` must outrank `status`** when both are present — a finished bg job whose process lingers otherwise reads as merely idle.
- **Transport:** newline-delimited JSON over `AF_UNIX`/`SOCK_STREAM` at `/tmp/cc-socks/<pid>.sock` (dir mode 0700). Receiver drops the connection if 1 MiB accumulates without a newline. Sender lingers ~150 ms after write before close on macOS; closing immediately risks a truncated frame.
- **Auth:** none on the wire. Receiver reads peer pid via `getPeerPid(fd)` → `verifiedPeerPid`; ancestor processes get tagged `selfSent`. Access control is purely filesystem permissions.
- **User frame:** `{"type":"user","message":{"role":"user","content":"…"},"priority":"next"|"now","from":"…","msg_id":"<uuid>","uuid":"<uuid>","session_id":"<uuid>"}`. `"now"` bypasses the serialization queue.
- **Always set `session_id`.** Socket paths are named by pid and **pids get reused** — the receiver's mismatch check is the only thing preventing misdelivery into a stranger's session.
- Injected text is wrapped ("Another Claude session sent a message: …") and filed with `origin.kind="peer"`, `isMeta:true`, **`skipSlashCommands:true`** — so slash commands cannot be injected this way.
- **Control frames:** only `rename` (with `name`) and `peer_message_status` (`orig_msg_id` + held|denied|expired|delivered) are acted upon. Everything else logs "Unhandled control action". **No abort/interrupt exists on this channel.**

## Background Daemon / Supervisor (2.1.226)

- `~/.claude/daemon/`: `control.key` (16-byte hex, 0600), `roster.json` (authoritative worker table), `attach-journal/`. Sockets at `/tmp/cc-daemon-<uid>/<daemonId>/`: `control.sock` (daemon-wide), `rv/<short>.sock` + `pty/<short>.sock` per worker (tokens `rvAuth`/`ptyAuth` in roster), `spare/` pre-warmed worker pool.
- Control protocol: newline-delimited JSON `{op, proto, auth}`. Ops: `ping list has reply kill attach resize dispatch respawn-stale lease leases await-ack shutdown yield`. Errors: `EAUTH EPROTO ENOJOB ENOREPLY ERESPAWNING EUNVERIFIED ESTARTING EUNKNOWN`. Rejects connections whose peer **uid** differs from the daemon's.
- Supervisor is **transient** — exits ~5 s after the last lease and bg job. Workers **survive** supervisor restarts (`bg adopt: adopted=N`); supervisor self-restarts on binary upgrade; parallel starts resolve by lockfile race.
- socat interposition on `rv/`/`pty/` yields **empty logs** — `claude attach` does not reach the worker through those filesystem paths.

## Turn Abort (verified)

No interrupt op on either channel. Working path: run `claude attach <short>` **on a real pty**, write ESC (`0x1b`), detach with two Ctrl+C. Confirmed: transcript gains `[Request interrupted by user]`, status `busy → idle`, session survives. `claude attach` is a full-screen TUI and cannot be driven through pipes.

## Binary Is Bun-Compiled — Source Is Readable

The npm `@anthropic-ai/claude-code` package is now only a thin installer that downloads a platform binary (no protocol logic). The installed binary is a **Bun single-file executable with the JS source embedded**, so `strings <binary>` exposes the full minified implementation — no MITM or syscall tracing needed for protocol work. Zod schema field names appear as consecutive short strings, which makes record shapes easy to recover.

## Job State Files

- `~/.claude/jobs/<short>/state.json`: `state`, `detail`, `tempo`, `inFlight{tasks,queued,kinds}`, `tokens`, `output.result`, `intent`, `respawnFlags`, `cliVersion`, plus `linkScanPath`/`linkScanOffset`.
- `linkScanOffset` shows Claude Code tails its own transcript JSONL **by byte offset** — copy that pattern instead of re-reading whole files.
- `~/.claude/jobs/<short>/timeline.jsonl` is only a coarse state-change log `{at,state,detail,text}` — transitions, **not** a block stream. For a block-level view parse the transcript JSONL.

## Peer Messaging Channel — Correction (2026-08-12, CC 2.1.228)

Die frühere Aussage „nur `kind: bg` bindet den Messaging-Socket, interaktive Sessions nie" (verified 2026-08-09 gegen 2.1.226) **gilt nicht mehr** und war schon damals aus dem falschen Grund richtig.

### Der Bind hängt an einem Remote-Feature-Flag, nicht an der Session-Art

```js
function Bb(){ if (platform === "windows") return false
               return flag("tengu_harbor_kite", false) || Boolean(env.CLAUDE_CODE_HARBOR_KITE) }
```

Die Skip-Meldung im Binary benennt es selbst:

```
[uds-messaging] Skipped: cross-session messaging gate off (will late-bind if a GrowthBook refresh enables it)
```

Also: eine Session, die **vor** dem Flag-Rollout gestartet wurde, hat keinen Socket und kann später einen bekommen; eine danach gestartete hat ihn von Anfang an. `kind` spielt keine Rolle.

### Belegt am 12.08. gegen 2.1.228

Registry-Aufteilung nach **Version**, nicht nach `kind`:

| pid | kind | entrypoint | Version | Socket |
|---|---|---|---|---|
| 2140 | interactive | cli | 2.1.223 | nein |
| 69096 | interactive | cli | 2.1.226 | nein |
| 24633 | interactive | cli | 2.1.227 | **ja** |
| 78733 | bg | cli | 2.1.225 | ja |
| 816 | interactive | sdk-cli | 2.1.228 | **ja** |

Ende-zu-Ende reproduziert: blankes `claude` auf einem PTY, `kind: interactive`, `entrypoint: cli`, ohne gesetzte Variable → bindet `/tmp/cc-socks/<pid>.sock`, nimmt eine Peer-Injektion an und antwortet auf dem Bildschirm.

`CLAUDE_CODE_HARBOR_KITE=1` erzwingt das Gate, wo das Flag noch nicht angekommen ist.

**Konsequenz für jeden Integrator:** Adressierbarkeit ist eine Messung am Feld `messagingSocketPath`, kein Merkmal der Session-Art — und sie kann während der Laufzeit erscheinen. Pro Poll neu lesen, nie über die Lebensdauer cachen.

**Methodische Lehre:** Reverse-engineerte Aussagen über Claude Code können hinter Remote-Flags stehen und damit ohne Release ungültig werden. Solche Befunde mit Version *und* Datum stempeln und periodisch nachmessen.

## Token-weiser Assistententext — welche Kanäle es gibt (2.1.228, gemessen)

| Kanal | Granularität | erreicht | Kosten |
|---|---|---|---|
| Transkript-JSONL | ganze Nachrichten | jede Session | schon da |
| `rv/<short>.sock` (rendezvous) | nur Zustand | Daemon-Worker | kein Text |
| `pty/<short>.sock` | Terminalstrom | nur Daemon-Worker | ANSI/DEC-Parser |
| stream-json über stdio | echte Deltas ~300 ms | nur selbst gestartete | man wird der Host |
| **`MessageDisplay`-Hook** | Deltas ~1 s | **jede Session** | Spawn pro Flush, in der Renderkette |
| OTEL-Logs | ganze Nachrichten | jede Session | Collector nötig |

### Das Transkript trägt nie Teiltext

Gemessen über 4000 Zeilen: 1312 assistant-Zeilen, 0 doppelte uuids, 0 Zeilen ohne `stop_reason`. Claude Code schreibt ausschließlich fertige Nachrichten; eine Zeile wird nie fortgeschrieben.

### `MessageDisplay` ist der einzige Kanal für fremde Sessions

Eines von 31 Hook-Events. Feuert pro Display-Flush mit:

```jsonc
{ "session_id": "…", "prompt_id": "…", "turn_id": "…", "message_id": "…",
  "index": 0, "final": false, "delta": "…" }
```

`index` zählt innerhalb einer `message_id` hoch, `final` schließt sie. Gemessen: 4 Flushes pro Antwort, ~1 s Abstand, absatzgroß.

**Achtung — der Hook sitzt in der Renderkette.** `appendChain.then(async () => output += await hook)`: die Anzeige wartet auf ihn, und er darf über `displayContent` ersetzen, was angezeigt wird. Ein langsamer Hook bremst das Terminal des Nutzers. Fehlerfall ist gutartig (`MessageDisplay hook flush N failed; displaying original delta`). Also nur winzige Binaries, die anhängen und enden — nie Netzwerk.

### stream-json ist eine vollwertige Session

`--print --input-format stream-json --output-format stream-json --include-partial-messages` liefert `content_block_delta`/`text_delta` alle ~250–400 ms. Der Prozess registriert sich als `kind: interactive`, `entrypoint: sdk-cli`, taucht in `claude agents --json` auf, schreibt das normale Transkript, bindet den Peer-Socket und nimmt weitere Prompts auf demselben Kanal an. `--resume <id>` behält die Session-id (kein Fork).

Dazu ein Steuerkanal (`control_request`): `set_model`, `set_permission_mode`, `interrupt` (mit `cancel_queued`), `rename_session`, `set_max_thinking_tokens`, `get_usage`, `get_context_usage`, `mcp_reconnect`. Damit sind Abort und Modellwechsel ohne PTY möglich — aber nur für selbst gestartete Prozesse.

### Rendezvous-Socket (`rv` = rendezvous)

Worker → Daemon: `state` (`patch:{tempo,detail,needs}`), `interactive-mark`, `repaint-done`, `shutting-down`, `heartbeat` (30 s).
Daemon → Worker: `auth`, `reply` (zweiter Injektionspfad), `repaint`, `attacher-caps`, `shutdown`.
Kein Inhalt — aber ein Push-Kanal für den Zustand, den man sonst pollt.

## Promoted (2026-08-15)

- [GOTCHA:0.95] Session-miner deduplicates against other mined entries but not against canonical content — many queued entries are pure duplicates of existing knowledge already in canonical topic files.
- [GOTCHA:0.85] Claude Code session JSONL schema requires per-entry fields: cwd, entrypoint, gitBranch, isSidechain, message, parentUuid, sessionId, timestamp, type, userType, uuid, version. Assistant message must include model, id, stop_reason, usage. Resume via `claude --resume <uuid>` only works if files are stored under `~/.claude/projects/<escaped-cwd>/`.
- [GOTCHA:0.92] Claude Code uses the nearest trusted ancestor directory (not cwd) as the project key in .claude.json when the actual project directory has no .git folder.
- [GOTCHA:0.90] A .git directory in the home folder containing only an allowed_signers file (not a real repo) is sufficient to make Claude Code treat the home directory as a project root, causing all sessions without a proper .git to be bucketed under ~/.
- [PATTERN:0.85] Derive the full MCP server set from three sources: (1) union of project disable-lists, (2) `claudeAiMcpEverConnected` in ~/.claude.json for claude.ai connectors, (3) plugin cache files (~/.claude/plugins/cache/.../.mcp.json) filtered by `enabledPlugins` in ~/.claude/settings.json, named as `plugin:<plugin>:<server>`.
- [GOTCHA:0.80] Docker Desktop Extension MCP servers (e.g. MCP_DOCKER, k8sgpt) have no definition source in ~/.claude and therefore only appear in the server list while they are disabled in at least one project.
- [GOTCHA:0.85] Do not delete a project entry from ~/.claude.json while a Claude Code session for that directory is still running — it will re-write the entry on exit.
- [GOTCHA:0.80] Deleting a project from ~/.claude.json removes all associated Claude Code state including allowedTools grants and onboarding status — warn the user explicitly before confirming.
- [GOTCHA:0.95] The CLI commands `claude respawn` and `claude rm` no longer exist in Claude Code 2.1.226; any UI or code referencing them will silently do nothing.
- [GOTCHA:0.90] The daemon operation `lease` keeps the daemon process alive, not an individual worker/session — it cannot be used to pin or keep a specific session alive.
- [GOTCHA:0.90] In Claude Code 2.1.226, isSidechain rows for subagents no longer exist; subagent transcripts are stored as separate files instead.
- [GOTCHA:0.90] In the Claude Code TUI, Ctrl+Z detaches to the shell but keeps the session running (state: waiting); Ctrl+C ends an idle session entirely.
- [CONVENTION:0.85] Claude Code TUI model changes via slash command are persisted as the default for new sessions immediately upon confirmation.
- [GOTCHA:0.90] In claude attach, Ctrl+C on a busy session is absorbed and the session survives, but on an idle session the second Ctrl+C is interpreted as 'exit', terminating the session. This asymmetry made the bug appear verified when it was not.
- [PATTERN:0.80] Model switching via /model command in background sessions works through the PTY channel (append text, send Return, confirm prompt) — the same channel used for abort signals.
- [GOTCHA:0.75] The CLAUDE_CODE_CHILD_SESSION environment variable suppresses session registration; match sessions by PID instead of environment variables to avoid false positives from foreign sessions.
- [GOTCHA:0.88] The Claude Code peer socket cannot be used to push messages to a connected client; the server's connectedClients set only supports add/delete/destroy, and the only outbound frame (peer_message_status) goes in the opposite direction — the channel is a mailbox, not a subscription.
- [PATTERN:0.75] Claude Code uses `inbound_origin` field to classify message sources; certain values map to `{kind:"human"}`, enabling distinction between human and peer-agent senders (related fields: `isRelayHuman`, `tengu_human_origin_presumed`).
- [GOTCHA:0.95] Claude OAuth credentials are scoped to the CLAUDE_CONFIG_DIR directory; they are NOT shared globally across different config directories. Each isolated config dir requires its own interactive /login flow.
- [GOTCHA:0.80] BLIP/Florence-2 auto-captioning scripts that call model.eval() are blocked by Claude Code security hooks interpreting eval() as code injection; replace with torch.inference_mode() context manager to avoid the false positive.
- [GOTCHA:0.90] systemMessage nudge hooks become noise when they fire in the wrong project scope — repeated false-positive nudges cause the model to learn to ignore them entirely, making advisory hooks counterproductive.
- [GOTCHA:0.90] macOS TCC (Transparency, Consent, Control) can silently revoke file-system permissions for Claude Code mid-session, causing all file reads, Bash commands, and git operations to fail with 'Operation not permitted' — even if they worked earlier in the same session.
- [PREFERENCE:0.95] CLAUDE.md should describe current state only, not evolution history; version references (chart >= 0.5.X, since 0.X.Y) belong in Git log or ADRs, not in project instructions.
- [PREFERENCE:0.90] Credential guard whitelist should include ~/.claude/** — files under Claude's own working set are not sensitive secrets and blocking reads there creates false positives.
- [GOTCHA:0.95] Anthropic API enforces streaming for long-running requests at request time — it calculates estimated duration server-side and rejects non-streaming calls immediately with a streaming-required error before any processing begins.
- [GOTCHA:0.90] hooks-config.json is merged back into settings.json by install.sh — disabling hooks only in settings.json is insufficient; both files must be updated synchronously or the hooks return on the next install.
- [GOTCHA:0.88] A hook registered in settings.json for a script that does not exist on disk fails silently with 'command not found' rather than raising an error — dead hook references go unnoticed.
- [GOTCHA:0.95] Using /bg in a running Claude Code session backgrounds the current session, killing the voice/channel connection — do not use it to spawn parallel background work from within a voice session.
- [GOTCHA:0.90] The /agents slash command opens the subagent-configuration dialog for managing .claude/agents/ definitions, not a dispatch mechanism for background sessions.
- [DECISION:0.95] To spawn a new background Claude session from within a running voice session, use `claude --bg "<prompt>"` via Bash — it starts a fresh detached process in parallel without disturbing the active session.
- [DECISION:0.90] Default model for background agents is Sonnet; Opus only when user explicitly requests deeper reasoning (e.g. architecture decisions).
- [DECISION:0.95] Claude Desktop voice mode uses a single full-duplex WebSocket per conversation (no WebRTC, no LiveKit); binary frames carry audio, JSON text frames carry control events.
- [PATTERN:0.90] Downstream TTS audio is PCM at 16 kHz, decoded in an AudioWorklet using a SharedArrayBuffer ring buffer with a 100 ms buffer for smooth playback.
- [DECISION:0.82] Claude Desktop uses Deepgram Nova-3 as the STT backend (identifiable from network traffic during dictation) and offers 5 TTS voices; read-aloud uses a separate WebSocket endpoint distinct from the conversational voice endpoint.
- [DECISION:0.90] Set cleanupPeriodDays: 36500 in ~/.claude/settings.json to permanently disable automatic transcript deletion, allowing the memory system to process the full conversation history.
- [GOTCHA:0.92] Claude Code's Cross-Session-Messaging (Agent Teams, enabled via CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1) is synchronous peer coordination between concurrently running sessions — it cannot deliver messages to sessions that do not yet exist. It is not a persistence mechanism and cannot replace async knowledge injection.
- [GOTCHA:0.85] Agent Teams team config is stored under ~/.claude/teams/{name}/config.json and is deleted when the session ends — it is ephemeral and unsuitable for any persistence requirement.
- [DECISION:0.90] Curator agent uses Adaptive Thinking + effort levels (off/low/medium/high/xhigh/max) instead of budget_tokens; effort maps to Go SDK constants BetaOutputConfigEffortLow/Medium/High/Xhigh/Max.
- [GOTCHA:0.90] Claude Desktop config only supports stdio/command transport — no http, streamable-http, or sse transports are valid there; the UI must restrict the transport field accordingly.
- [PATTERN:0.85] When mutating Claude Code or Claude Desktop config files, read fresh before each write, mutate only MCP keys, write atomically via temp-file+rename, and keep a rotating backup (last 10) under ~/.claude/backups/ to avoid data loss.
- [GOTCHA:0.85] Claude Code creates a projects entry in ~/.claude.json for every directory it has ever been started in, including ~ and ~/Documents — these are harmless but will appear in any tool that lists all projects.
- [GOTCHA:0.90] claude-in-chrome MCP server is globally enabled via `claudeInChromeDefaultEnabled: true` in ~/.claude.json and uses opt-out logic (disabledMcpServers), while computer-use uses opt-in logic (enabledMcpServers) — opposite polarities for built-in servers.
- [GOTCHA:0.85] LSP tools require exact line/column positions for hover, definition, and references — you typically need to read the file or use workspaceSymbol to locate the position first, which reduces the speed advantage over grep.
- [PATTERN:0.85] For destructive MCP tools, use SDK `createElicitation` to prompt the user for confirmation with a concrete impact summary (e.g. 'Delete board including 12 tasks'); clients that do not support elicitation receive a `confirm: true` fallback parameter that the tool checks before proceeding.
- [GOTCHA:0.95] mcp-remote does not automatically reconnect with a new initialize handshake after a server restart; it keeps POSTing with the stale session ID, causing persistent '404 Invalid session ID' errors until the mcp-remote process itself is restarted.
- [GOTCHA:0.90] MCP servers running with WithStateful(true) for lazy tool-group loading store session IDs in RAM only; any container restart invalidates all active sessions, making stateful MCP servers incompatible with zero-downtime redeploys unless session state is externalized.
- [GOTCHA:0.88] mcp-remote sends notifications/initialized with an empty session ID during its own handshake, which triggers format validation errors in the log — these are benign but look identical to the real bug and cause false alarm investigation.
- [GOTCHA:0.80] After context compression, MCP sub-tools that were successfully callable earlier (e.g. nc_deck_create_card) may report 'not found' even though the loader claims they are registered — function injection does not survive a context reload.
- [GOTCHA:0.90] When an MCP server sends `tools/list_changed` globally, any connected client re-fetches its tool list and loses tools that were dynamically unlocked via loader calls — producing misleading 'not found' errors that look like client-side bugs.
- [GOTCHA:0.85] A credential-guard hook that pattern-matches 'password' or 'token' in grep command lines will also fire on innocent search terms — switching to the Read tool to access the same file is still a hook bypass and is not permitted without user consent.
- [GOTCHA:0.85] Claude/LLM token budget must account for reasoning tokens separately; ignoring reasoning tokens causes silent budget overruns and truncated outputs.
- [GOTCHA:0.90] A Claude Desktop MCP server receiving EOF on stdin (client closed connection) must exit with code 0 — treating EOF as an error causes spurious non-zero exits that mislead process supervisors.
- [CONVENTION:0.88] Replace `--local-timezone` CLI flags in MCP server configs with `env.TZ` in the JSON config; `TZ` is evaluated first in Go's timezone lookup chain and achieves the same explicit zone selection.
- [CONVENTION:0.82] Claude Desktop config requires an absolute path for the `command` field of an MCP server entry; relative paths are not resolved.
- [GOTCHA:0.75] A `bin/` directory that is gitignored and cleaned by `make clean` will cause MCP server configs pointing into it to break silently after a clean build — document this risk prominently.
- [GOTCHA:0.95] Claude Desktop reinitializes MCP connections with a new session ID every few minutes or per turn/poll; any server-side state (e.g. revealed tool sets) keyed on session ID is lost on every reconnect, making session-bound tool visibility fundamentally unworkable with Claude clients.
- [GOTCHA:0.90] Claude Desktop cannot reach 127.0.0.1 MCP endpoints directly from the Anthropic cloud connector; mcp-remote proxy (npx mcp-remote) remains required for local MCP servers even when Claude Desktop supports Streamable HTTP natively.
- [PATTERN:0.85] For MCP servers used with Claude Desktop, implement restart-resilience tests that verify tools/list returns the full tool count and that tool calls succeed with a completely unknown session ID — these are the two scenarios Claude Desktop exercises in production.
- [GOTCHA:0.95] Displaying token usage as a running sum across turns is misleading: cache reads are counted each turn, so a session with 100 requests can accumulate 34 million 'total' tokens while the conversation grew by only 56 k. Show new tokens per turn for display; total for billing only.
- [DECISION:0.88] Cache pricing has two TTL tiers: read at 0.1×, write at 1.25× (5-minute TTL) and 2× (1-hour TTL). The transcript separates both TTLs, so cost accounting must distinguish them rather than treating cache write cost as a single rate.
- [DECISION:0.95] Using `claude --resume <id> --background` to resume sessions forks the session (new session ID, history copied) — the Claude Code daemon owns the process and survives agent restarts, trading ID continuity for robustness over a PTY held by the agent.
- [GOTCHA:0.80] Claude Code screenshot tool masks content from apps that have not been granted screen recording permission, making their windows appear as blank/empty regions in transcripts rather than showing an error.
- [GOTCHA:0.90] The actual number of available MCP servers in a Claude Code session can exceed the configured count because plugins and connectors contribute additional servers that appear in no config file; count from the session transcript's deferred_tools_delta attachment, not from the config.
- [GOTCHA:0.80] There is no reliable on-disk signal for whether an MCP server is currently running; tool count should be represented as nil (unknown) rather than 0 when no source has reported it.
- [PATTERN:0.80] Subagent transcripts in Claude Code are stored at <session-id>/subagents/agent-<agentId>.jsonl and should be loaded lazily on first expand to avoid loading long transcripts for runs that are never inspected.
- [GOTCHA:0.85] A hook may block foreground `sleep` commands (standalone sleep calls); running the same command via run_in_background bypasses the block and completes successfully.
- [GOTCHA:0.80] The Claude Code peer socket carries undocumented fields including file_attachments, a third priority value 'later', and cross_session_inbox_auth — the protocol is richer than official docs suggest.
- [GOTCHA:0.90] The PTY socket (`pty/`) exists only for daemon/background worker sessions, not foreground terminal sessions. Counting active sockets at a given moment reflects usage patterns, not a protocol limitation — foreground sessions do get PTY sockets when observed via `claude agents`.
- [GOTCHA:0.85] When splicing only a single event key in a JSON/config file, operating at the key level (not the block level) is required to keep the round-trip byte-identical; block-level edits reformat surrounding content and break byte-neutrality.
- [GOTCHA:0.85] Peer messages delivered via the Claude Code peer-wrapper never include a resolvable sender address in the `from=` field, making it impossible to reply via SendMessage without knowing the peer's identity in advance.
- [GOTCHA:0.80] Messages injected via PTY arrive in the User-Turn slot with no peer wrapper, origin marking, or `from=` field, making them indistinguishable from directly typed user input.
- [GOTCHA:0.95] Messages sent over a local Unix socket peer channel are always stamped with origin kind 'peer' based on process credentials — the 'inbound_origin' and 'client_platform' fields are only read by the cloud bridge, so peer-channel messages cannot be made to appear as human input regardless of frame content.
- [GOTCHA:0.85] When reading transcript/archive files to extract metadata (e.g. cwd, branch), only reading the first lines will miss data written later in the file — session metadata can appear at byte 95,000+.
- [GOTCHA:0.85] Claude Code background-job harness enforces worktree isolation (blocks Edit on the main checkout) at the tool level — this cannot be disabled via settings.json; the CLAUDE.md instruction 'Never call EnterWorktree' conflicts with this harness behavior in background sessions.

## Headless `-p`: nur die Deny-Seite begrenzt (gemessen 2026-09-01, CC 2.1.252)

Wer die CLI aus einem Wrapper heraus startet und die Werkzeuge einschränken
will, kann sich auf `--allowedTools` **nicht** verlassen. Drei Läufe, gleicher
Prompt („lege verboten.txt mit dem Write-Tool an"), gleiches Modell:

| Flags | Datei geschrieben? | `permission_denials` |
|---|---|---|
| `--allowedTools Read` | **ja** | `[]` |
| `--permission-mode manual --allowedTools Read Grep Glob` | **ja** | `[]` |
| `--disallowedTools Write Edit NotebookEdit` | nein | `[]`, Modell meldet „Tool ist deaktiviert" |

Ursache ist die ambiente `~/.claude/settings.json`: Ein Kindlauf erbt sie, und
auf der Maschine standen dort `"defaultMode": "auto"` und 149
`permissions.allow`-Regeln. `--allowedTools` **ergänzt** diese Liste, statt sie
zu ersetzen, und `--settings` lädt laut `--help` ausdrücklich nur *additional*
settings — also kann keins von beidem zurücknehmen, was schon erlaubt ist.

Konsequenz für jeden Wrapper: `deny = bekannte_tools − angefragte_tools`
berechnen und als `--disallowedTools` übergeben. Die Liste der bekannten Tools
gehört in Konfiguration, nicht ins Binary — ein CLI-Release, das ein Tool
hinzufügt, reißt sonst ein Loch in die Grenze.

**`permission_denials` ist kein Nachweis.** Das Feld blieb auch in dem Lauf
leer, der tatsächlich blockiert wurde. Es taugt nicht als Signal dafür, dass
eine Beschränkung gegriffen hat.

## `--session-id` und `--resume` sind komplementär und scheitern laut (2.1.252)

| | Session existiert | Session fehlt |
|---|---|---|
| `--session-id` | exit 1, `Session ID … is already in use.` | legt sie an |
| `--resume` | setzt fort | exit 1, `No conversation found with session ID: …` |

Beide Fehlgriffe brechen ab, statt etwas Unbeabsichtigtes zu tun. Ein Aufrufer,
der die Session-ID selbst wählt (etwa als UUIDv5 der Issue-URL, damit der
Orchestrator keinen eigenen Zustand braucht), darf deshalb raten und einmal im
anderen Modus wiederholen — das kostet einen Prozessstart, nie eine falsche
Antwort.

Als schneller Pfad lässt sich das Transkript prüfen:
`~/.claude/projects/<absoluter Workdir, / durch - ersetzt>/<uuid>.jsonl`.
Beobachtet an 2.1.252, kein dokumentierter Vertrag — nichts darauf aufbauen,
was bei geändertem Layout falsch statt langsam würde.

Weiteres, gemessen am selben Tag: `-p` liest den Prompt von **stdin**, wenn kein
Prompt-Argument steht (wichtig, damit fremder Text nicht in `ps` landet), und
`--resume` arbeitet mit `-p` und `--output-format json` zusammen.
