# anki-chat-dock — Design

Status: design draft, pre-implementation. Last updated: 2026-07-02.

An Anki add-on that presents a collapsible, gorgeous chat dock/sidebar where the user talks to an AI agent. The agent's context prominently features the card currently under review, it can query the collection through tools, and it can propose new notes that follow the user's conventions.

This document is the plan. It records the recommended architecture, the decisions still open, and the known flaws/risks so implementation can start without re-deriving them.

---

## 1. Product summary

- **Surface**: a collapsible dock (Qt `QDockWidget` hosting a webview) attached to the Anki main window. Available during review and at the deck browser. Toggled/focused with a single configurable chord; the same chord returns focus to the reviewer.
- **Core loop**: user chats with an agent. When a card is being reviewed, that card (fields, note type, deck, tags, scheduling state) is prominently in context. The obvious use case is "discuss this card", but arbitrary questions work.
- **Agent tools**: read tools over the collection (Anki search syntax, note/card fetch, deck/tag/stat overviews) allowed by default; write tools (note creation) gated behind proposals unless auto-accept is enabled. Permission modes control all of this.
- **Collection awareness**: the agent receives a cached, annotated description of the collection — full deck hierarchy and full tag hierarchy with note counts, card counts, and review-time buckets (today / 7d / 90d / all-time) — refreshed by an in-process background job.
- **Note creation**: the agent proposes notes matching the user's conventions; the user reviews/edits/accepts in the dock. Deck, tags, note type, and prefilled fields can be pinned by the user and persist until changed. Conventions are supplied as an Agent Skill (prompt-only or full skill with assets).

## 2. The backend question: BYOK API vs. CLI agents

**Decision update (2026-07-05, user-confirmed): the add-on is harness-based, period.** The backends are agent harnesses — **Claude Code, Codex, and Pi** (`@earendil-works/pi-coding-agent`) — and the in-add-on BYOK APIBackend (old M4) is **dropped**. BYOK is instead served by handing user-provided API keys to a harness through its environment (implemented: `anthropic_api_key`/`openai_api_key` pasted into config — explicitly the less-secure option — or `*_op` 1Password references resolved via `op read` at spawn, secret never touching disk; empty = the harness's own OAuth login). Pi is the lightweight BYOK path: npm-installable (`npm i -g --ignore-scripts @earendil-works/pi-coding-agent`), no subscription needed, API-key/OAuth auth.

Per-harness adapter facts (researched + locally probed 2026-07-05):

- **Claude Code** (shipped): headless stream-json, MCP over our localhost HTTP server. Web access now allowed by default (`web_access` config; WebSearch/WebFetch in allowedTools, off switches them to disallowed) and the `Skill` tool is allowed so system-wide user skills and our template skill work.
- **Codex** (M3): `codex exec --json` with MCP config. Local probe found the volta-installed codex broken (missing platform vendor binary, spawn ENOENT) — exactly the class of environment failure the doctor panel must diagnose.
- **Pi** (M3): modes `-p`, `--mode json` (event stream: `agent_start/turn_*/message_update{text_delta,toolcall_delta}/tool_execution_*/agent_end`), `--mode rpc` (JSONL over stdio with session resume/fork). **No built-in MCP** — tools are added via pi's TypeScript extension API, so our adapter ships a small pi extension that bridges pi tool calls to the add-on's MCP HTTP endpoint (same registry, same server-side permission enforcement). System prompt via per-project `SYSTEM.md`/`AGENTS.md` in the agent workdir; skills load from `~/.pi/agent/` and project dirs. Local probe: JSON mode works structurally (`{"type":"session"}` handshake) but the machine had no API key configured for pi — the BYOK plumbing above is the fix.
- **Distribution (decided 2026-07-05): the add-on does not concern itself with agent installation at all.** No auto-install of anything (even Pi assumes npm, which users may not have). The doctor panel simply reports what is and isn't installed and links to each harness's own install page.

Original analysis below kept for the record; its "BYOK direct-API backend as a later milestone" recommendation is superseded by the harness-key approach above.

> **Design a `ChatBackend` interface from day one. Ship the CLI-agent backend (Claude Code first, Codex second) as the MVP. Add the BYOK direct-API backend as a later milestone.**

### Why CLI-first

1. **The agent loop comes free.** Claude Code / Codex already implement the hard parts: multi-turn tool-use loop, context management, retries, streaming, model selection, session resume. A BYOK backend means reimplementing all of that inside an add-on, and maintaining it as APIs evolve.
2. **Tools come free via MCP.** Both CLIs speak MCP. The add-on exposes its collection tools once, as an MCP server; both CLIs consume them with zero per-CLI tool code. A BYOK backend needs a hand-rolled tool-execution loop.
3. **Agent Skills only fully work here.** The "full agent skill with scripts/assets" feature for note conventions is only *executable* by a CLI agent (it can run the scripts). Under BYOK, a skill degrades to prompt text — scripts would need a bespoke sandboxed runner, which is out of scope.
4. **Subscription economics.** Users with Claude Pro/Max or ChatGPT plans pay nothing extra. BYOK metered API costs are a real adoption barrier and a support burden ("why did my key get charged $30").
5. **It matches how this workspace already works** (Claude Code-first learning workflow, per the 2026-06-24 workspace decision). The add-on becomes a thin, beautiful window onto an agent the user already trusts, instead of a second bespoke agent.

### Why not CLI-only

1. **Install friction.** "Have Claude Code or Codex installed and logged in" breaks the "as simple as any AnkiWeb add-on" bar for most users. BYOK-with-a-key is the more conventional add-on experience (cf. every TTS/AI add-on on AnkiWeb).
2. **Environment fragility.** CLI discovery (PATH inside a GUI app on macOS is not the shell PATH), version drift, login/session expiry, per-CLI flag changes. Needs a "doctor" panel that diagnoses this in one click.
3. **Latency.** Process spawn + CLI startup adds seconds vs. a direct API call. Mitigation: keep one persistent session process per chat, not one per message.

### Consequences for the design

- `ChatBackend` protocol: `start_session(context) -> Session`, `Session.send(user_msg) -> stream of events` (text delta, tool-call started/finished, permission request, proposal, error, done), `Session.cancel()`, `Session.close()`.
- The **tool registry is backend-neutral**: plain Python functions with JSON-schema signatures. The MCP server (CLI path) and the BYOK tool loop (API path) are two thin adapters over the same registry. This keeps the BYOK door open without designing for it twice.
- Backend choice is per-profile config; the UI is identical either way.

### CLI integration mechanics (Claude Code as reference)

- Spawn `claude` in headless streaming mode (`-p --output-format stream-json --input-format stream-json --include-partial-messages`) as a **persistent subprocess per chat session**; multi-turn via the stream-json input, session resume via `--resume` if the process must be restarted. `--include-partial-messages` gives token-level text deltas, so the dock streams exactly like the terminal does. Process spawn latency (~1–3s) is masked by **pre-warming when the chat gains focus** (e.g. via `Cmd+J`) — an idle process waiting for input costs nothing, it happens once per chat session rather than per review, and a hidden/unfocused dock still spawns nothing.
- Maintenance surface: the stream-json event schema is the *only* CLI format we parse (transcripts persist our own normalized events; the session id is opaque). It is the documented headless/SDK interface and evolves mostly additively, but drift is a standing cost: one thin per-CLI adapter maps raw events → our event schema, the doctor panel checks pinned minimum CLI versions, and a workbench smoke test runs against the real installed CLI to catch breakage in CI rather than in users' reviews.
- System prompt injection via `--append-system-prompt` (card context, collection overview, conventions). Model and reasoning effort are per-session CLI flags (`--model <alias|id>`, `--effort low|medium|high|xhigh|max`), surfaced as a header picker in the dock and persisted per-profile. Switching mid-chat keeps the conversation: the session tracks the model/effort its live process spawned with, and on the next send respawns the CLI with `--resume <session-id>` plus the new flags (applied lazily so an in-flight response is never interrupted) — the same "switch model mid-conversation" behavior the CLI apps have.
- Tools via `--mcp-config` pointing at the add-on's MCP endpoint, `--allowedTools` / permission flags mapped from the add-on's permission mode. Restrict the CLI's own file/bash tools by default — the agent should live in collection-land, not the user's filesystem, unless the user opts in.
- Skills: the add-on materializes the user's convention skill into a temp skills dir and points the CLI at it.
- Codex: same shape via `codex exec` with its JSON output mode and MCP config. Abstracted behind the same `CLIBackend` with a per-CLI adapter table (binary discovery, flags, event parsing). **Validation task for the Codex milestone**: confirm whether `codex exec --json` exposes token-level message deltas; if not, Codex degrades gracefully to paragraph-at-a-time rendering.

### MCP transport

MCP here is **only the wire protocol** between the CLI subprocess and the add-on's own in-process tools — it is how Claude Code / Codex are told "these tools exist, call them here". It is not a product feature and not a general-purpose Anki API.

The add-on runs a **minimal MCP server over localhost HTTP** inside Anki's process (background thread, random port, per-session bearer token). Hand-rolled JSON-RPC — no heavy dependencies, AnkiWeb-friendly. For CLIs that only speak stdio MCP, ship a tiny stdio↔HTTP bridge script inside the add-on package and register that as the MCP server command. Tool execution marshals onto Anki's main thread (`mw.taskman.run_on_main`) because collection access is not thread-safe.

**Prior art — existing Anki MCP servers (not reused).** Several exist (e.g. `ankimcp/anki-mcp-server`, `nailuoGG/anki-mcp-server`, `CamdenClark/anki-mcp-server`); all are standalone processes that proxy to the AnkiConnect add-on. We do not reuse them because:

1. They add two install dependencies (AnkiConnect + a Node/Python server) — breaks off-the-shelf simplicity.
2. Our write path must route through the ProposalManager (proposal cards, pins, ledger, auto-accept caps), not AnkiConnect's direct `addNote`.
3. Permission modes must be enforced server-side in our layer, per chat session; a generic proxy has no notion of them.
4. Our tools are opinionated (clue-based `find_related`, budgeted annotated trees, cached stats), not raw CRUD.

What we do take from them: tool naming/schema conventions where they exist, so agents' priors transfer.

**Context-frugal tool advertising (tried 2026-07-05, quantified, then dropped same day).** The skills-style idea — advertise brief name+description, serve full docs via a `tool_help` tool — was implemented and measured: 20 tools, full listing ≈ 2.0k tokens, compact ≈ 1.6k, savings ≈ 380 tokens (19%), with the incompressible schema floor (~1.2k tokens, 60%) dominating because MCP has no lazy-schema mechanism and models cannot call tools without schemas. **Removed** on review: each `tool_help` round-trip adds a tool call + result to the context (easily exceeding the 380 tokens saved), and mid-conversation doc fetches churn the prompt cache — the "optimization" plausibly costs more than it saves. Full descriptions are served; there is no tool_help tool.

## 3. Architecture

```
┌─ Anki process ──────────────────────────────────────────────┐
│  Qt main window                                             │
│  ├─ QDockWidget "Chat"                                      │
│  │   └─ AnkiWebView (chat UI: HTML/CSS/JS, no build step)   │
│  │        ⇅ pycmd bridge (JSON messages)                    │
│  ├─ ChatController (Python)                                 │
│  │   ├─ ContextAssembler   (card ctx, overview, clues)      │
│  │   ├─ ToolRegistry       (backend-neutral tools)          │
│  │   ├─ PermissionEngine   (modes, per-call prompts)        │
│  │   ├─ ProposalManager    (note proposals, pins, ledger)   │
│  │   └─ ChatBackend                                         │
│  │        ├─ CLIBackend  ── subprocess: claude / codex      │
│  │        └─ APIBackend  ── HTTPS (BYOK, later)             │
│  ├─ MCP server thread (localhost HTTP, token-auth)          │
│  └─ StatsCache job (taskman background + QTimer, N min)     │
└──────────────────────────────────────────────────────────────┘
```

- **No separate daemon.** Everything the daemon was imagined for (periodic stats) is an in-process background job: `QTimer` schedules, `mw.taskman.run_in_background` computes, results cached to `user_files/` as JSON with a computed-at timestamp. A daemon would complicate install, updates, lifecycle, and multi-profile handling for no benefit. Revisit only if stats queries measurably jank the UI on huge collections (they shouldn't: revlog aggregation is one SQL pass).
- **Chat UI is a webview, not Qt widgets.** "Gorgeous, slick, modern" is achievable in HTML/CSS with Anki's bundled `AnkiWebView`; native Qt widgets would fight us. Vanilla JS + CSS custom properties (no npm build step — keeps AnkiWeb packaging trivial), light/dark theming synced from Anki's theme hook. Streaming markdown rendering with a small vendored renderer.
- **All collection access on the main thread**; all subprocess I/O on reader threads; events marshaled to UI via the bridge.

## 4. Context assembly

Every session (and on relevant changes mid-session) the agent receives:

1. **Current card block** (when reviewing): note type name, deck path, tags, all fields (name → value), question/answer state, scheduling info (due, interval, ease, lapses, reps), plus the card's recent revlog. Updated when the reviewer moves to a new card — sent as a context-update message, not a new session.
2. **Clues block**: machine-extracted hints for finding related cards — field prefixes (e.g. `Analysis:` from "Analysis: define limits", detected as `^\w[\w\s]{0,30}:` patterns that repeat within the note type), the tag list, the deck path components. Presented as *hints with instructions*, e.g. "cards in this collection often share a prefix; try `Front:Analysis:*`".
3. **Collection overview** (from the stats cache): deck tree and tag tree, each node annotated `notes / cards / review-time today | 7d | 90d | ever`. Plus totals, note type list with counts, and cache age. *Delivery changed 2026-07-10 (COMPLIANCE.md rule 3):* no longer inlined into `--append-system-prompt`; it now travels **once per session** as a `<collection-overview>` block prefixed to the first user message (`controller._overview_for_send` → `context.wrap_user_message`), and rides message history across `--resume` respawns instead of being retransmitted. The system prompt keeps only a fallback sentence for when the cache is empty.
4. **User conventions skill** (see §7). *Same 2026-07-10 change:* no longer inlined; materialized as a real agent-home skill the harness auto-discovers, pointed at from the system prompt by one fixed sentence.
5. **Standing instructions**: what tools exist, permission mode in force, how to propose notes, how to use Anki search syntax effectively. This — plus the pins block — is all that remains in `--append-system-prompt`, deliberately bounded (< 4,000 chars worst-case, test-guarded) so the appended prompt stays lean and generic.

**Size control — measure, then decide.** Most users' deck/tag hierarchies are small; don't degrade them preemptively. The serializer renders the full annotated trees, estimates token count (chars/4 heuristic is fine for a threshold decision; exact tokenizers aren't available offline), and:

- **Fits the budget** (default ~8k tokens, configurable): include both trees in full. Expected common case.
- **Over budget**: fall back to card-local context — the current card's deck lineage (ancestors + siblings), its tags, and a top-level skeleton of both trees with fold annotations (`… +k subdecks, m notes`) — and rely on tools for everything else. Config override: `auto` (default) / `always-full` / `minimal`.

Either way, `deck_tree` / `tag_tree` tools always return complete trees on demand. For choosing relevant tags/decks from a large list, the standing instructions recommend the **subagent pattern**: a CLI-backend agent spawns a subagent to read the full tree and pick relevant nodes, keeping the main context clean. An LLM reading the actual list beats keyword or embedding search at this scale — no embedding infrastructure is planned. (BYOK equivalent later: a one-shot cheap-model "tree picker" call behind the same tool.)

## 5. Tools

Backend-neutral registry; names and schemas identical across MCP and BYOK paths.

Read (default-allowed):
- `search_notes(query, limit)` — Anki search syntax; returns note ids + key fields snippet.
- `get_note(id)` / `get_card(id)` — full fields, tags, deck, model, scheduling.
- `deck_tree(prefix?)`, `tag_tree(prefix?)` — drill into the annotated hierarchies beyond the overview budget.
- `collection_stats(scope)` — cached stats; deck- or tag-scoped.
- `list_note_types()` / `get_note_type(name)` — fields, templates (for convention-following).
- `find_related(card_id, strategy?)` — convenience wrapper that applies the clue heuristics (prefix/tags/deck/keywords) and merges results.
- `get_card_images(card_id?|note_id?)` — returns the card's embedded images as **MCP image content blocks** (base64 from `collection.media`, capped count/size, local files only — remote/`data:` sources skipped), so the model actually sees the picture rather than the `<img src>` filename. The current-card context block flags when a card has images and points the agent here. Audio is deliberately out of scope: the model cannot ingest audio, so a `[sound:...]` reference would need a transcription step. The MCP server passes a tool result straight through when it is already a list of content blocks, else wraps it as one text block.

Write (proposal-gated):
- `propose_note(note_type, deck, tags, fields, rationale)` — never writes directly; creates a proposal card in the UI (see §8).
- `propose_note_edit(note_id, field_changes, add_tags, remove_tags, rationale)` — same gate; `field_changes` maps field name → new value. Validation includes a **staleness guard**: the proposal carries the field values the agent last read, and if the note changed underneath (user edited mid-chat, sync), the proposal is flagged for re-review instead of applying blind.
- (later) `propose_tag_change(...)`, `propose_deck_move(...)` — same pattern.
- Deck management (create/rename/options) and filtered decks: see §16.

Permission modes (per-profile setting + per-session override in the dock header):
- **Default**: reads always allowed, writes via proposals.
- **Ask each read**: every tool call shows an inline approve/deny chip (for the cautious).
- **Read-only**: write tools not even advertised to the agent.
- **Auto-accept**: proposals apply immediately (see safeguards in §8).

For the CLI backend, modes map onto CLI permission flags *and* are enforced server-side in the MCP layer — the MCP server is the actual security boundary, the CLI flags are just UX.

**MCP scoping — restrictive by default, opt-in to widen (decided 2026-07-10, config-file tier shipped 2026-07-10).** The dock spawns the CLI with `--strict-mcp-config` and a config containing only our `anki` server by default, so the agent sees *our* tools and nothing else — not the user's own Claude Code MCP servers. This is deliberate isolation: card/field content is untrusted model input (§13.3), so silently wiring every MCP server the user configured for coding into a context that also ingests untrusted card text is an exfiltration surface (a malicious shared deck could steer the agent into calling their filesystem/GitHub/etc. server). Default safe, same posture as the deliberately-unshipped unrestricted tier (§5).

Widening is now implemented as a **config-file-only tier** (`chat_with_your_cards/backends/claude_cli.py: build_cli_args` / `write_mcp_config`; config keys documented in `config.md`) — no settings-panel UI yet, edit `config.json` by hand:
- `mcp_servers` (default `{}`): extra dock-specific servers, Claude-Code server-spec JSON, merged verbatim into our `--mcp-config` alongside the built-in `anki` server.
- `mcp_inherit_user` (default `false`): drops `--strict-mcp-config` so the user's own Claude Code MCP servers load too.
- `mcp_disabled` (default `[]`): server names (ours or inherited) added to `--disallowedTools` as `mcp__<name>`, individually turning any of them back off.

Guards (both ignore-and-log, never a hard error): a user-supplied `mcp_servers["anki"]` entry can never shadow the built-in server (`write_mcp_config` drops it and assigns the real one last); `"anki"` in `mcp_disabled` is dropped rather than honored, since disabling it would silently break every `propose_*`/collection tool. A GUI for this (discoverable toggles in the settings panel, replacing hand-editing `config.json`) is still a later milestone.

**Graduated power tiers and bulk actions (proposed and confirmed 2026-07-04; implemented same day except the unrestricted tier, which stays deliberately unshipped):**

- **Bulk single-op tools** (default tier, proposal-gated): some "bulk" operations are one semantic op in Anki — `rename_tag` (`col.tags.rename`), `find_and_replace`, `move_cards_to_deck`. Each renders as *one* proposal card ("Rename tag X → Y — affects 1,243 notes"). No new permissions needed.
- **Change sets** (default tier and up): for semantic sweeps ("fix this subtle problem across 1,000 cards"), the agent opens a change set, streams per-note edits into it, and the UI shows one card with the count, sampled diffs for spot-checking, and an expandable full list. Accept/reject applies the whole set: forced `col.create_backup` checkpoint first, chunked main-thread application (the 15s marshal timeout applies per chunk, not per set), one ledger entry, one-click revert. Review-at-scale = audit a sample + description, not 1,000 cards.
- **trusted-writes tier** (new mode): direct write tools (update/add/delete/bulk) without proposal cards. Safeguards: backup checkpoint at first write of a session, every touched note ledgered + provenance-tagged, a per-session write budget that pauses for confirmation when exceeded, deletes always confirmed.
- **agent-tools axis / environment power** (`agent_tools`: `sandbox` default | `full`; Slice 1 shipped 2026-07-13): a **separate, orthogonal axis** from the collection-write permission mode above — that axis gates *collection* writes; this one gates the CLI's own Bash/file tools. `sandbox` (default) keeps the current posture: `--disallowedTools Bash,Edit,Write,NotebookEdit`, no `--permission-mode`. `full` drops those disallows and adds `--permission-mode bypassPermissions` (auto-approve — headless `-p` has no interactive prompt, so a shell/file call would otherwise be refused). Both share the identical MCP-scoping / web / model-effort-fast flags, and switching respawns the CLI with `--resume` on the next message, exactly like a model switch. Sharp edge: card/field content is untrusted model input (§13.3), so `full` turns a malicious shared deck's prompt injection into **immediate** arbitrary code execution — there is no per-command gate. This is surfaced honestly: the constraint line in `--append-system-prompt` and the agent-home `CLAUDE.md` are both **conditional** on the mode (they never claim shell is off when it is on, or vice-versa), and the dock shows a dismissable amber risk line plus a "What's the risk?" modal spelling out the injection danger. Claude Code's built-in circuit breaker (`rm -rf /`, `rm -rf ~`) and deny rules still apply. **Honest note:** `full` makes the collection-write safety chokepoint (proposals/review) an *informed, bypassable default* — a shell can route around it — so the mode is documented as trust-scoped and is not sticky-safe on shared collections.
  - **Slice 2 (investigated 2026-07-13, not viable in current CLI — do not build yet):** the intermediate Claude Code permission modes (`default` / `acceptEdits` / `plan`) that would surface *per-command* approvals in the dock, between sandbox and full-bypass. A feasibility spike against the real `claude` 2.1.207 headless (`-p` stream-json) established this is not cleanly achievable: **(a)** the "prompting" modes don't prompt in headless — there's no interactive terminal to prompt through, so `--permission-mode default`/`manual` just *runs* the tool (empirically: a Bash `echo` executed, `permission_denials: []`); **(b)** the only interception hook, `--permission-prompt-tool`, is **absent from 2.1.207's `--help`**, its request/response contract is officially undocumented (open upstream issue), and in a live test — a real MCP approval server wired to return `{"behavior":"deny"}` — the command **still ran**, i.e. the hook did not demonstrably fire. Building on it would mean reverse-engineering an unadvertised, unstable contract that my one empirical test suggests doesn't even work. Conclusion: in headless the genuinely-achievable granularity is exactly the two states Slice 1 shipped (sandbox = tools off; full = tools on, auto-approved); the "ask per command" middle ground is an interactive-terminal feature with no stable headless equivalent today. Revisit only if a future CLI ships a documented, stable permission-prompt contract. The UI's "Agent tools" section is still structured so such modes could slot in as additional items. Collection-power and environment-power remain deliberately separate axes.
- Implementation notes (2026-07-04): `rename_tag` / `find_replace` / `move_cards` / `delete_notes` (trusted-only, always confirmed, non-ledger-revertible — backup checkpoint instead) plus `open_change_set` / `add_to_change_set` / `close_change_set`. Change-set accept ports the workspace's AnkiConnect safety patterns: per-item staleness snapshots (the `pushedHash` idea — changed notes are skipped and reported, never overwritten blind) and a before/after note/card-count comparison that surfaces unexpected drift (e.g. conditional-card activation) as warnings on the resolved card. Bulk applies force `col.create_backup` first. Trusted-writes consumes a per-session `write_budget` (default 200 notes); exhaustion falls back to gated proposals with a notice.
- Rationale vs. raw AnkiConnect / third-party Anki MCP proxies: a power user can always bypass us, so the top sanctioned tier must be strictly better — undo/ledger integration, session grouping, backup checkpoints, injection-aware gating — leaving no reason to route around the add-on.

## 6. Stats cache

- Background job every N minutes (default 30, configurable) and on demand (manual refresh button; debounced hooks on sync/deck changes).
- Computed via a few SQL passes: `notes`/`cards` grouped by deck; tag expansion from `notes.tags`; revlog time bucketed by day cutoffs for today/7d/90d/ever.
- Persisted per-profile to `user_files/stats_cache/<profile>.json` with schema version + computed-at. Served to context assembly and to `collection_stats`/`*_tree` tools with an explicit staleness annotation so the agent can say "as of 12 minutes ago".
- Extension point for "possibly more stats": retention %, new/day, mature/young split — add behind the same cache, never computed synchronously in the render path.

## 7. Note conventions as an Agent Skill

Two tiers, both stored per-profile in `user_files/skills/note-conventions/`:

1. **Prompt tier**: user writes/pastes a conventions prompt in the config UI; the add-on wraps it into a minimal `SKILL.md`.
2. **Full-skill tier**: user drops a complete skill directory (SKILL.md + references + scripts). CLI backends get it mounted as a real skill (scripts runnable, subject to CLI permissions). The BYOK backend inlines `SKILL.md` (+ referenced markdown) into the system prompt and **ignores scripts, with a visible warning** — executing user scripts without a CLI agent's sandbox is out of scope.

The existing `ai-enhanced-learning` skill (`skills/anki-card-authoring/`) is the first real-world test fixture.

**Skills strategy (expanded 2026-07-11).** Because the add-on runs the user's own system agent, that agent already picks up the user's system-wide skills (e.g. `~/.claude/skills`, pi's `~/.pi/agent/`) for free — we do nothing. The add-on contributes a layered Anki skill system under `user_files/agent-home/.claude/skills/` (pi equivalent when that adapter lands): (1) `anki-card-authoring` owns individual retrieval design and wording; (2) `anki-curriculum-design` owns goals, topic decomposition, prerequisites, coverage, and streams; (3) `anki-curriculum-delivery` owns safe live rollout through tags, normal/filtered decks, proposals, and verification; (4) `note-conventions` is the user-specific override layer; and (5) `skill-maintenance` learns recurring card-authoring preferences from edit observations. Keeping curriculum design separate prevents collection mechanics from silently changing scope, while keeping delivery separate prevents a pedagogical plan from implying unsafe or unsupported Anki operations. Factory task skills are neutral defaults, not Elvis's conventions. Each file is seeded only when absent and becomes user-owned immediately, so adding a new factory skill upgrades existing installs without overwriting customized files. The `Skill` tool is in the CLI's allowedTools so skills actually fire headless.

**Agent environment / tool limits (added 2026-07-12; made mode-conditional 2026-07-13).** In the default `sandbox` agent-tools mode the dock spawns the CLI with `--disallowedTools Bash,Edit,Write,NotebookEdit` (card content is untrusted input → RCE risk, §5), and that disallow propagates to subagents (verified: a subagent's `Write` failed). But the agent wasn't *told* this, so it assumed it had shell via a subagent, tried to write a file, failed, and flip-flopped (dogfood). Fix has two channels: a tight one-line constraint in `--append-system-prompt` (guaranteed main-agent delivery, kept under the rule-3 length ceiling), plus a fuller `user_files/agent-home/CLAUDE.md` (`skills.materialize_agent_environment`, regenerated each run) — Claude Code loads CLAUDE.md from cwd for the main agent *and* subagents, so it's the one place the limits reach a subagent. In sandbox the agent is told to hand the user a recipe when a task needs code execution or media generation, rather than attempting it. **Both channels are now conditional on `agent_tools` (§5):** in `full` mode the shell/file tools are actually on, so the same two channels instead tell the agent it *has* shell/write, warn that card content is untrusted (injection risk), and steer it to the proposal tools and AnkiConnect (never direct `.anki2` writes) — they must never lie about the live tool posture in either direction. Length ceiling (< 4,000 chars) is re-tested for both modes. **Session-preamble freshness — intentional Claude-Code parity (decided 2026-07-13):** the system prompt and CLAUDE.md are captured at session start (per Anki run for CLAUDE.md), so a *mid-session* sandbox↔full switch (which respawns with `--resume`) does **not** rewrite them until the next chat/launch — same as model/effort/permission-mode. This deliberately mirrors Claude Code's own model: a continued/resumed conversation keeps the preamble it was born with rather than retroactively rewriting it, and switching the model there doesn't re-read CLAUDE.md either. We do not special-case this: a fresh chat always gets the correct text, which is the same escape hatch Claude Code offers. (We *could* regenerate the append-prompt/CLAUDE.md on our respawn, but chose parity over a bespoke divergence — the switch is rare and the next chat is correct.)

**Conventions delivery (updated 2026-07-10, COMPLIANCE.md rule 3).** Previously the resolved prompt-tier conventions text was inlined into `--append-system-prompt` and only the `anki-card-authoring` template lived in the discovered agent-home directory. Now the resolved conventions (config prompt wins, else the `user_files/skills/note-conventions/SKILL.md` body) are **also mirrored** into `user_files/agent-home/.claude/skills/note-conventions/SKILL.md` (`skills.materialize_conventions_agent_skill`), where the harness auto-discovers them like any other skill; the system prompt carries a one-line pointer instead of the text. Unlike the hand-editable templates, this mirror is regenerated every run — its source of truth stays `user_files/skills/` / the config field. Tradeoff accepted: conventions now depend on the agent loading the skill (same mechanism `anki-card-authoring` already relies on) rather than being unconditionally in context; if dogfooding shows proposals ignoring conventions, the fallback is re-inlining a trimmed version.

## 8. Proposals: note creation and note editing

Flow: agent calls `propose_note` → ProposalManager validates (note type exists, deck exists or is creatable, required fields present, duplicate check via Anki's dupe detection) → proposal card renders in the chat stream with editable fields, deck picker, tag editor, Accept / Edit / Reject.

**Editing proposals — the review UX is a flagship surface.** The bar is "Cursor-grade amazing", but the right interface differs because the artifact is a flashcard, not code:

- **Field-level diffs on rendered text**: word-level inline highlights (deletions struck through, insertions marked) per field — not line-based code diffs. Unchanged fields collapsed.
- **Live card preview, before/after**: the proposal renders the note through its *actual card templates* — a toggle (or side-by-side, width permitting) between current card and card-as-it-would-become. Seeing the real card is the flashcard equivalent of Cursor showing the real file, and it's the detail most likely to make the UX feel magical.
- **Granular acceptance**: per-field accept/reject plus accept-all, like Cursor's per-hunk controls.
- **Keyboard-first**: when a proposal has focus — `Cmd+Enter` accept, `Cmd+Backspace` reject, `Tab`/arrows move between fields/proposals. Multiple pending proposals form a queue navigable without the mouse.
- **Reversible**: the session ledger stores each edit's prior field values; one-click revert per edit and "revert session". An undone proposal keeps a **Re-add** button (re-creates the note / re-applies the edit) so an accidental undo is one click to reverse.
- **Live edit preview**: editing a proposal's fields debounces a `proposal_preview` request; the ProposalManager re-renders the card through the real templates and pushes a `preview_update`, so the before/after preview tracks what the user is typing (the active tab is preserved across updates).
- **Proposals behave like artifacts**: when the agent revises a still-pending proposal it passes `supersedes=<id>` (instructed in the system prompt), and the old card is set to **Superseded** — deactivated/dimmed with a **Restore** button — rather than lingering as a second pending card. A **Suggest change** button on each pending card seeds the composer with a reference to that card so the user can ask the agent to revise it.
- **Batch controls**: when two or more proposals are pending, an **Accept all / Reject all** bar appears above the composer (each accept still applies that card's in-place edits).
- **Reviewer stays fresh**: after any write (accept, revert, undo, re-add), the ProposalManager calls an `after_write(note_ids)` hook; if the reviewer is showing one of those notes it reloads and re-renders in place, so an accepted edit shows without leaving and re-entering review.
- **Tool calls read as plain language**: the transcript maps `mcp__anki__*` calls to friendly labels ("Searched your cards", "Read a note", …), hides raw JSON payloads and internal CLI plumbing (ToolSearch), and hides `propose_*` chips since the proposal card itself is the visible artifact.
- **Auto-accept scope**: auto-accept mode applies to *creations only* by default. Edits mutate existing user content and stay proposal-gated unless a separate, explicit auto-accept-edits toggle is enabled (same cap + ledger safeguards).

**Pinning**: a dock panel lets the user pin deck, tags, note type, and prefilled field values, through Anki-editor-like selectors styled to the dock — deck and note-type dropdowns populated from a `collection_meta` event (deck names, note types + their fields, existing tags) pushed on web-ready, a space-to-commit tag chip editor with `<datalist>` autocomplete, and per-field default inputs that render from the selected note type's actual fields. Pins **persist automatically** on every change (no Save button); "Clear all" resets them. Pinned tags are a **floor, not a ceiling**: they are always added, but the agent may still propose additional tags, and the user edits the final tag set per-proposal on the card (below). A stricter "pinned tags exclusive" mode is a possible future toggle.

**Proposed destination is editable**: create-proposal cards carry an editable deck dropdown (from `collection_meta`) and an editable tag chip editor pre-filled with the proposed tags, so the user reviews and changes where the note lands and how it's tagged before accepting; the chosen deck/tags flow through on accept.

**AI-provenance tags are configurable** (not hardcoded): `created_tag` (default `ai-created`) on created notes, `edited_tag` (default `ai-edited`) on accepted edits, and a `session_tag_prefix` (default `ai-chat-dock::session-`) for the per-session tag that powers the Browser-jump and undo-session actions. Any of them set to empty disables that tag; all three empty means AI notes get no automatic tags. Pins are injected into context as *constraints*: pinned deck/tags are applied to every proposal (agent told not to fight them); pinned note type restricts `propose_note`; prefilled fields are defaults the agent should keep unless it has strong reason (and must flag when it overrides).

**Auto-accept mode** safeguards (this feature is the riskiest in the product):
- Every AI-created note is tagged `ai-created` (+ `ai-chat-dock::session-<id>`), matching the existing workspace convention.
- A session ledger records created note ids; the dock offers one-click "review this session's notes in the Browser" and "undo session" (batch delete while unstudied).
- Per-session cap (default 20) before auto-accept pauses and asks to continue.
- Validation failures always fall back to a manual proposal card, never silently dropped.

## 9. UI / UX

- **Dock**: right-side `QDockWidget`, collapsible, **docked-only** (movable between the left/right edges but not floatable — a torn-off floating panel strands the user and steals focus; user-decided 2026-07-06), width persisted. The top header is chat-management only: new-chat, history, open-in-Claude-Code (a split button with a caret opening a Desktop-app / Terminal chooser), and doctor. Following Claude Code, the **permission-mode chip and model/effort picker live in a control row inside the composer** (mode + Pins bottom-left, model/effort + send bottom-right), not the header. The permission-mode chip opens a dropdown to pick a mode directly (Shift+Tab still cycles). Model/effort and Pins open as cards floating just above the composer. **Tools menu**: a "Chat With Your Cards" submenu with verb-labeled entries and their shortcuts ("Open / focus chat" · Ctrl+J, "New chat" · Ctrl+Shift+J) that call the same actions as the chords — replacing the old bare checkable dock-title toggle, which was confusing (just the add-on name, and its show/hide semantics diverged from the focus-aware chord).
- **Look — copy the vernacular, don't innovate (decided 2026-07-02)**: the chat surface deliberately mimics Claude Code / ChatGPT conventions, because users (including the author) already have that muscle memory and those UIs are good enough in a worse-is-better sense. Concretely: bottom-pinned rounded composer with send button inside it; stop button replaces send while streaming; streaming markdown with code blocks + copy buttons; tool calls as collapsed, expandable rows inline in the assistant turn (query + result count visible); new-chat control at the top; history as a plain session list. The novelty budget is spent only on Anki-specific surfaces: the context chip and proposal cards. CSS custom properties keyed to Anki's palette; obeys light/dark and night-mode hooks. Design pass via workbench screenshots (see §11).
- **Shortcuts** (all configurable in config UI, registered as `QShortcut` on the main window). Proposed defaults:
  - **`Cmd+J` / `Ctrl+J` — toggle chat focus**: if dock hidden → show + focus input; if focus in dock → return focus to reviewer/deck browser; if focus elsewhere and dock visible → focus input. One chord, three context-aware behaviors. Home-row, one-handed, unused by stock Anki (reviewer, deck browser, or editor). The webview forwards the chord back to Python when the input has focus (Qt shortcuts don't fire inside webviews reliably — known sharp edge).
  - **`Cmd+Shift+J` / `Ctrl+Shift+J` — new chat** with fresh context, **focus stays in the chat composer** (clear-and-keep-typing); also a header button. Previous session goes to history.
  - **`Esc`** (in chat input) — context-aware, mirroring Claude Code: while a response is streaming it stops generation; otherwise it returns focus to the reviewer/deck browser without hiding the dock.
  - **`Shift+Tab`** (in chat input) — cycle permission modes, exactly as in Claude Code.
  - **`Enter`** sends, **`Shift+Enter`** newline.
  - **`Tab`** (in empty composer) — accept the suggested question (below).
- **Suggested questions (ghost text)**: when the composer is empty and focused, a context-aware suggestion renders as gray ghost text; `Tab` accepts it into the composer, typing anything dismisses it. Start with **static templates** chosen by context — reviewer: "Explain this card to me — I forgot", "Why is this the answer?", "Find cards related to this one"; deck browser: "What should I study today?", "Which decks are getting rusty?" — rotating between visits. Static first because it's instant, free, and predictable; **generated** suggestions (cheap model, cached per card) are a later experiment behind a flag, since per-card generation adds latency, cost, and distraction risk to every review. Configurable off.
  - Fallback chords if `J` clashes with another add-on: `Cmd+;` (home row, essentially never taken) or `Cmd+K` (familiar from launcher/chat UIs, but collides with "insert link" muscle memory in many editors).
  - Config UI warns on known conflicts with reviewer keys (space, 1–4, `u`, `e`, `*`, `-`, `!`, etc.) and stock main-window shortcuts (`a`, `b`, `y`, `t`, `s`, `d`).
- **States**: the chat itself always starts empty — **no assistant preamble, no auto-greeting**; the conversation begins when the user sends the first message. Context (card block or collection overview) is assembled lazily at first send; the CLI process pre-warms when the chat gains focus, so spawn latency is hidden well before the first send (a hidden/unfocused dock spawns nothing). The only pre-chat chrome is a subtle context chip showing *what the agent will see* ("Context: card 'define limits' in Math::Analysis" / "Context: collection overview") — trust through transparency, not a message in the transcript. The chip updates as the user moves between reviewer, deck browser, and overview.
- **Frontend stack — assistant-ui, dev-time build, committed bundle (decided 2026-07-10)**: the chat surface is being rebuilt on `@assistant-ui/react` (in `ui/`, Vite build → self-contained IIFE `bundle.js`/`bundle.css` committed to `web/next/`), replacing the hand-rolled vanilla-JS `web/`. Rationale: assistant-ui ships stop, collapsible reasoning, tool cards, branch/fork, and generative-UI (our approve/reject proposal card is its canonical HITL example) as first-class primitives — the hand-rolled versions were reimplementing solved problems, and two dogfooding complaints (no stop button, no reasoning indicator) were runtime-layer gaps the UI can now surface once the backend feeds them. **Both backend gaps landed 2026-07-11** (`chat_with_your_cards/backends/claude_cli.py`): a `ThinkingDelta` event (now carrying `estimated_tokens` alongside `text`) is emitted on `content_block_start` for a thinking block (opens the UI's indicator immediately) and on every subsequent `thinking_delta` stream event, regardless of whether its text is empty. Thinking itself is **adaptive at `--effort high`** (only appears when the prompt actually warrants it) and **consistent at `--effort max`**, per live CLI probes (2.1.207, stream-json + `--include-partial-messages`) — but the thinking *text* is redacted upstream at every level observed so far (`delta.thinking` stays `""` throughout; only the opaque, encrypted `signature_delta` carries real content), so `estimated_tokens` — not non-empty text — is what actually proves a thinking phase is live. Both UIs drive a rotating "Thinking…"/"Reasoning it through…" indicator with a live `~N tokens` suffix off that field (classic UI: `app.js`'s `ensureThinkingIndicator`/`renderThinkingPhrase`; next UI: `ReasoningBlock.tsx`, via a non-empty `THINKING_SENTINEL` placeholder so assistant-ui's `fromThreadMessageLike` doesn't drop the empty-text reasoning part outright), collapsing to a static "Thought for ~N tokens" once the turn moves past the thinking phase (or disappearing entirely if no token estimate was ever reported); and `ClaudeCliSession.interrupt()` sends a real `control_request`/`control_response` (wire framing ported from the Claude Agent SDK's `Query._send_control_request`, MIT-licensed) so **Esc-to-stop keeps the process and conversation alive** instead of `cancel()`'s teardown-and-respawn-with-`--resume` — live-verified against the real CLI, with `cancel()` as the fallback on any failure (no live process, write error, ack timeout, or explicit error subtype) so the stop button can never wedge. A bridge adapter maps our existing `ChatEvent` vocabulary onto assistant-ui's external-store runtime and mirrors the current `pycmd` contract, so the Python side is otherwise unchanged. **Dock loading (decided 2026-07-10)**: Anki loads it via the *same* `stdHtml()` path as today — a `<div id="root"></div>` body fragment plus `bundle.js`/`bundle.css` registered as web exports — keeping Anki's night-mode class, the pycmd bridge, and the ready handshake `bridge.ts` expects. The standalone `web/next/index.html` the build emits is a **dev-only** artifact for `npm run dev` browser iteration; Anki never loads it. Guardrail: the bundle mounts on `DOMContentLoaded` so React doesn't run before `#root` exists. Restyle (the Claude-Code-adjacent-but-own visual language, per §9's "copy the vernacular" note) is a later pass against the live preview.
- **Frontend is now single-UI — classic deleted, markdown landed (decided 2026-07-11)**: the restyle above ("Reading lamp") is done and reviewed, so the hand-rolled vanilla-JS UI (`web/app.js`, `web/styles.css`, `web/index.html`, `web/vendor/marked.min.js`) and its `dev/preview.html` browser harness were **deleted outright**, and the `ui: "classic" | "next"` config flag was removed — there is nothing left to switch between. `dock.py`'s `_load_ui` now unconditionally loads the assistant-ui bundle from `web/next/` through the same `stdHtml()` path (a `<div id="cwyc-root"></div>` body fragment plus `bundle.js`/`bundle.css` web exports; the standalone `web/next/index.html` stays a dev-only artifact Anki never loads). The one real capability gap the classic UI had — **markdown rendering** — was closed first: assistant text is rendered as **sanitized markdown via `marked` (18.0.5) → DOMPurify (3.4.11) → `dangerouslySetInnerHTML`** (`ui/src/markdown.ts`, `TextPart.tsx`), matching the classic `marked.js` posture but adding the sanitize step the classic UI lacked (model output is untrusted — a shared deck can carry text aimed at the assistant). It is streaming-safe: `renderMarkdown` runs on every `text_delta` and marked tolerates partial markdown, with a try/catch falling back to escaped text so a mid-stream render can never throw or blank the turn. Both deps bundle at build time (zero runtime network). The dev loop is now `cd ui && npm run dev` (see §11). The "copy the vernacular" note (§9 above) and the "Reading lamp" signature moments (proposal-card flip, thinking ember — see `ui/README.md`) are unchanged.
- **Chat history**: sessions persisted per-profile as **our own event-sourced JSON transcripts** in `user_files/` — every rendered event (user/assistant messages, tool calls + results, proposal cards and their outcomes, permission decisions, context-chip state) is the source of truth for display and history. Each transcript stores the backend's session id, so "continue this chat" maps to the backend's native resume (`claude --resume <id>`) while pixels come from our file. CLI session files are deliberately *not* parsed: their formats are internal and version-unstable, the BYOK backend has none, and they lack our UI-level events. Reopening Anki starts a fresh chat by default (the previous one is in History); the opt-in `restore_last_chat` config reopens the most recent chat automatically (same replay + `--resume` path as picking it from History).
- **Fast mode (added 2026-07-13)**: headless "fast mode" (Opus-only faster output) is enabled ONLY by spawning the CLI with `--settings '{"fastMode": true}'` (claude CLI `>= 2.1.205`) — there is no flag, no env var, and no mid-session toggle upstream. It therefore rides the exact same respawn-on-next-message path as a model/effort switch: `config.fast_mode` (default `false`) flows through `ClaudeCliBackend`/`ClaudeCliSession` (`_fast_mode`/`_spawned_fast_mode`, compared alongside model/effort in `_ensure_process`) and `ChatController.set_agent_config(model, effort, fast_mode)`, which persists it and re-pushes `push_agent_state()`'s `"agent"` payload with a `fast: bool` field. UI: a "Fast mode" On/Off section in the Model/effort picker (`ComposerControls.tsx`'s `ModelPicker`), reflecting `ui.agent.fast` and showing it in the button label (e.g. "Opus · high · fast").
- **Context-window usage (added 2026-07-13)**: the stream's `usage` object carries only cumulative per-turn token counts — `input_tokens`/`output_tokens`/`cache_creation_input_tokens`/`cache_read_input_tokens` (Anthropic API usage field names). "Context used" for a turn is approximated as `input_tokens + cache_read_tokens + cache_creation_tokens` (the size of that turn's own request, since each call resends the growing conversation as its input — no manual summing across turns). The footer shows a compact `128k / 1M context` readout with a thin proportional bar (amber fill, turning the warning red past ~80% full).
  - **Window size — live value preferred, table as fallback (refined 2026-07-13 via the Slice-2 spike).** The `result` message *does* carry the real per-turn window in a `modelUsage` map (`modelUsage.<model-id>.contextWindow`, e.g. `1000000` — dogfooded against 2.1.207, contra the earlier "no window in stream" belief). `_context_window_from_result()` (`backends/claude_cli.py`) pulls it, picking the token-**dominant** entry's window so a smaller-window subagent doesn't skew the main-thread gauge; it flows through `UsageUpdate.context_window` → the `usage` payload → `UsageFooter.tsx`, which **prefers it** over the hardcoded table. The per-model tables — `context_window_for()` (Python, unit-tested) and its TS port `ui/src/contextWindow.ts` — remain as the **fallback** for scripted/older backends that omit `modelUsage` (**1,000,000** for Opus 4.6/4.7/4.8, Sonnet 4.6/5, Fable 5, Mythos*; **200,000** for Sonnet ≤4.5, Haiku ≤4.5, and unrecognized). Net effect: the "unpinned model → pessimistic 200k default" caveat is gone whenever a real turn has run; the table only bites before the first result or on backends without it.
- **Fast-mode verification (added 2026-07-13)**: the `result` message also reports `fast_mode_state` (`"on"`/`"off"`) — the CLI's ground-truth of whether the running process actually engaged fast mode. It flows through `UsageUpdate.fast_mode_state` → the `usage` payload → the store's `UsageSnapshot.fastState`, verifying that the requested `--settings '{"fastMode":true}'` took effect rather than trusting the arg shape alone. (No dedicated UI badge yet; it's observability/state, and is the authoritative source for the *effective* fast state of the current process should a future badge want to distinguish requested-vs-effective across the respawn boundary.)

## 10. Packaging & compatibility

- Single AnkiWeb-standard add-on: pure Python + bundled web assets + the stdio↔HTTP MCP bridge script. No compiled deps, no post-install steps. **On the npm build (clarified 2026-07-10):** "no build step" means *on the user's machine / at install time* — the assistant-ui frontend (§9) builds at **dev** time into a committed, self-contained `web/next/bundle.js`/`bundle.css`, which ships as ordinary bundled web assets. AnkiWeb never sees Node. `user_files/` for all mutable state so AnkiWeb updates don't wipe it.
- Anki 25.x+ (Qt6 only). macOS/Linux first-class; Windows expected to work but CLI discovery there is a known validation task.
- Config: Anki's add-on config for simple settings + a custom config dialog for shortcuts, pins, skill editing, backend doctor.
- Release via `anki-addon-release` when the time comes.

## 11. Development workflow

(Revised during M0 — the original plan assumed host GUI iteration via `anki-workbench launch`; that proved both broken and intrusive on macOS.)

- **Fast UI loop, no Anki**: `cd ui && npm run dev` — the assistant-ui frontend's Vite dev server (http://localhost:5173) renders the real components (`ui/src/`) against a scripted replayer (`ui/src/dev/`) with a stubbed `pycmd`; type `tool`/`propose`/`edit`/`think`/`long`/`error` to drive each event path. Iterate design/CSS/JS here in seconds, invisibly. (The old `dev/preview.html` harness for the hand-rolled `web/` UI was retired 2026-07-11 when the classic UI was deleted — see §9.)
- **Real-Anki verification, headless**: `make test-gui-smoke-docker` (workbench Docker/Xvfb). The probe drives the real send path end-to-end and captures light+dark screenshots itself via `mw.grab()` — no OS screenshot permissions, no visible windows. Treat screenshots as design evidence (text fits, controls discoverable, dock legible in both themes).
- **Real-collection stress suite (added 2026-07-05).** The probe also drives the *real* `ProposalManager` and collection tools against the disposable collection — the real-Anki counterparts of the fake-collection unit tests: `col.tags.rename`, `col.set_deck`, `col.remove_notes`, `col.update_note`, `col.create_backup`, the media dir, and reviewer refresh in real review state. **What is and isn't automatable, made explicit:** every collection-*mutation* path is deterministic and belongs here, because it runs on the real ProposalManager regardless of which backend is talking. The one thing that genuinely stays manual is the real Claude CLI's *behavior* (does it pick the right tools, follow the skill, write good cards) — non-deterministic and paid, so `dev/cli_live_check.py` covers a single round-trip and the rest is human QA. This suite immediately paid for itself: it found that the backup checkpoint created **no backup in real Anki** — `create_backup(force=True, wait_for_completion=False)` returned before writing and a swallowed exception hid failures. Fix: checkpoints before *irreversible* deletes are now **synchronous** (`wait_for_completion=True`, on disk before the delete proceeds); reversible ops (ledger-undoable) stay async best-effort; and the result/exception is captured (`state.last_checkpoint`, backend log) instead of silently swallowed. The fake-collection unit tests couldn't catch this because they only assert the injected checkpoint *callable* fires.
- **Never launch visible Anki GUIs on the user's machine during normal work** (user feedback 2026-07-02: focus stealing disrupts their other work). Both pain points were fixed upstream in anki-addon-workbench 0.4.2 the same day: host smoke/launch on macOS now default to **stealth mode** (helper add-on: show-without-activating, lowered, parked off-screen except a 2px sliver — full occlusion would throttle QtWebEngine and break `mw.grab()`), and `launch` no longer needs xdotool on macOS (startup-marker wait on the stdout log). Visible runs are opt-in via `--foreground`.
- A fake backend (`ScriptedBackend` replaying canned event streams) so UI/UX iteration never needs a live CLI or API key, and so smoke tests are deterministic.
- M0 sharp edges hit (for future reference): pycmd is not wired the instant page JS runs (ready ping must retry until acked); QtWebEngine leaves stale tiles after a pure CSS-class theme flip (force a reflow before `mw.grab()`); `resizeDocks` only sticks on visible docks (apply width in `showEvent`); Anki's global `content-box` broke the composer (scoped `border-box` reset).

## 12. Milestones

- **M0 — Scaffold**: add-on skeleton, dock with webview, pycmd bridge, workbench smoke green, ScriptedBackend streaming fake chats. *Design iteration starts here.*
- **M1 — Claude Code MVP**: CLIBackend (Claude Code), MCP server + bridge, read tools, card context + clues, stats cache + overview, toggle/new-chat shortcuts, permission modes (default + read-only).
- **M2 — Notes** *(landed 2026-07-03)*: propose_note + propose_note_edit, proposal cards with field diffs + before/after card preview + keyboard-first review, pins, conventions skill (prompt tier), session ledger + undo/revert, auto-accept (creations) with safeguards.
- **M3 — Polish on the Claude Code backend** *(rescoped 2026-07-05; substantially landed same day)*: ✅ chat history/resume (event-sourced transcripts in `user_files/transcripts/`, history panel, replay + native `--resume`; pending proposals from an old session render as set-aside on replay); ✅ suggested questions; ✅ permission-mode chip + Shift+Tab with server-side enforcement; ✅ live context chip; ✅ cost/usage display; ✅ doctor-lite panel (harness/op presence + versions on a background thread, broken-install detection, install links); ✅ **open-in-Claude-Code** (header button hands the chat to a terminal Claude Code via `--resume` in agent-home; `agent-home/.mcp.json` is rewritten each run with the MCP url+token — 0600 — so the terminal instance keeps the anki tools *and* gains Claude Code's full toolset). ✅ **ask-each-read mode** (opt-in for untrusted shared decks): every read tool call blocks its MCP thread on an inline Allow/Deny chip via an ApprovalBroker (120s timeout = deny; teardown unblocks-as-deny; writes are not double-gated — proposals already review them). ✅ **Open in GUI Claude Code**: the button is now a chooser (Desktop app / Terminal). Desktop uses the documented `claude://code/new?folder=…&q=…` deep link — the app cannot resume a session by id yet (upstream feature requests open), so the handoff opens a new session in agent-home with a prompt pointing at this chat's transcript file, which the GUI agent reads for context; the terminal path keeps true `--resume`. Config dialog dropped in favor of in-dock panels.

**Document reading (researched + decided 2026-07-05).** The Claude Code `Read` tool opens **PDFs natively** — page ranges, pages rendered visually, **figures included** — so no bespoke PDF tool is needed (the "script" previously observed was the separate pdf-manipulation skill, not reading). Bespoke tools are reserved for formats the harness can't read: **`read_epub`** (pure stdlib — EPUB is a zip of XHTML: container→OPF→spine parsing, chapter listing with titles/sizes, per-chapter text extraction capped at 60k chars, chapter figures returned as MCP image blocks). **MOBI/AZW deliberately unsupported** (proprietary binary); the tool's error tells the agent to have the user convert via Calibre. "Agent-powered parsing tools" (a sub-agent inside the tool) rejected as overkill: the agent itself reading the right pages *is* the agent-powered parser.

**Source position metadata (v2, user direction "a JSON of sorts").** Two conventions, both parsed by `get_card_sources` into a `meta` object: the standard `#page=N` URI fragment, and a `data-source='{...}'` JSON attribute on the anchor (chapter, section, quote, anything). The system prompt and the card-authoring skills instruct the agent to *write* rich anchors when creating source-grounded cards — `<a href="URI#page=N" data-source='{"chapter": "…"}'>title, p.N</a>` — so future sessions jump straight to the right spot. Renamed/moved files still just fail to resolve (accepted).
- **M-later — Harness breadth**: Codex adapter (`codex exec --json` + MCP config; note the locally probed volta-install breakage for the doctor) and Pi adapter (`--mode json`/RPC + a pi tool-bridge extension proxying to our MCP server — the main new engineering).
- **M4 — Distribution & polish** *(replaces the dropped BYOK APIBackend milestone)*: AnkiWeb packaging and listing, harness install guidance in onboarding, cost/usage visibility from harness usage events, and the AnkiWeb-audience decision (Pi + pasted key is the low-friction path for non-subscribers).
- **Card sources — decided + v1 shipped 2026-07-05** (design by user): **any URI in any field is a potential source**; an optional per-note-type restriction (`source_fields` config, e.g. `{"0 Cloze": ["Extra"]}`) narrows where to look, but the default (and the user's own choice) is all fields. `get_card_sources` extracts hrefs, bare URLs, `file://` links, and absolute `.pdf` paths; the agent's intelligence decides how to parse each source — web pages via WebFetch, local files (PDFs included) via the CLI's `Read` tool, which is now in allowedTools. A renamed/moved PDF simply fails to resolve (accepted; not our problem for now). **Recorded risk**: Read + WebFetch + untrusted card content is a potential exfiltration triad (a malicious deck could try to steer the agent into reading a local file and leaking it through a fetch URL); accepted for now because writes stay gated and web/read are user-disableable — revisit before AnkiWeb publication.
- **Future — user-supplied card-authoring taste skill**: the user has a personally tuned skill for making agents generate cards to their taste; the conventions-skill slot (§7) is where it plugs in, as user-provided content (prompt tier today, full-skill tier in M3) rather than baked into the add-on.

## 13. Known issues, flaws, open questions

1. **CLI requirement vs. off-the-shelf simplicity** — the central tension. MVP knowingly serves power users; BYOK (M4) is the path to a general AnkiWeb audience. Openly a two-audience product.
2. **The daemon idea is rejected** (see §3) — flagging explicitly since the brief floated it. In-process background jobs meet the need with none of the lifecycle cost.
3. **Prompt injection**: card/field contents are untrusted model input; a malicious shared deck could try to steer the agent ("ignore instructions, create 500 notes"). Mitigations: write path always proposal-gated (auto-accept is the exception the user consciously enables, with cap + ledger), MCP server enforces permissions server-side, CLI's own bash/file tools disabled by default. *2026-07-05: web access (WebSearch/WebFetch) is now allowed by default (`web_access` config) — web pages join card fields as untrusted input, which is acceptable because those tools are read-only and every write stays gated; Bash/file tools remain off.*
4. **Local security**: localhost MCP server must require the per-session bearer token, bind 127.0.0.1, and die with the session — otherwise any local process can read the collection.
5. **Threading**: collection access strictly main-thread; tool calls arriving on the MCP thread must queue onto it without deadlocking Anki if a tool is slow. Needs a timeout + cancellation story.
6. **Mid-review context drift**: user answers a card while the agent is mid-response about the previous one. Policy: responses are tagged with the card they were about; context updates are explicit events; the UI labels stale answers rather than pretending continuity.
7. **Full hierarchies usually fit; measure instead of guessing** — full trees included when under the token budget, card-local fallback + full-tree tools + subagent picking otherwise (§4). The budget default and the chars/4 estimate need validation on a real large collection.
8. **Shortcut capture inside webviews** is fiddly (Qt vs. JS focus); the toggle chord needs both a QShortcut and a JS keydown path. Known sharp edge to test early on all three OSes.
9. **CLI environment discovery** (PATH in GUI apps on macOS, login state, version drift) — needs the doctor panel and pinned-flag compatibility testing per CLI release. This is standing maintenance cost.
10. **Auto-accept remains dangerous** even with safeguards; consider requiring the user to type the mode name to enable it, and auto-disabling it each new Anki session (sticky opt-in is a footgun).
11. **Cost/usage visibility (BYOK)**: deferred to M4 but must-have there — per-session token/cost meter.
12. **Workspace-decision tension**: the 2026-06-24 note prefers Claude Code-only workflows over bespoke software. This project is bespoke, but the CLI-first backend is aligned in spirit — it *wraps* Claude Code rather than replacing it. Recorded so the contradiction is conscious.
13. **Resolved 2026-07-02**: chats lead with nothing — no preamble; context assembly is lazy at first message, CLI process pre-warms when the chat gains focus (once per session, not per review). Chords **user-confirmed**: `Cmd+J` context-aware toggle (returns focus when in dock), `Cmd+Shift+J` new chat keeping focus in composer, `Esc` stop-or-leave. Chat UX copies Claude Code / ChatGPT conventions verbatim (§9). Transcripts: own event-sourced JSON with embedded backend session id for native resume (§9) — CLI session files never parsed, so the only CLI-format maintenance surface is the documented stream-json interface (§2).
14. **M2 implementation notes (2026-07-03).** The ProposalManager is the single write path; agent tools submit to it, the webview's proposal cards accept/reject through it, and everything runs on Anki's main thread (tool calls are already marshaled there). The staleness guard snapshots the changed fields at *submit* time and re-checks at *accept* time; on mismatch the proposal refreshes its baseline and re-renders for re-review instead of applying. Per-field acceptance ships in v1 (checkbox per changed field); granular per-field reject-with-comment did not. Previews render via `Note.ephemeral_card()` + `render_output()` with the model CSS, shipped as HTML into a sandboxed iframe (best-effort: proposals degrade gracefully to fields-only when rendering fails). Demo/smoke coverage uses a `ProposalRequest` backend event that only the ScriptedBackend emits; the controller routes it into the real ProposalManager, so the Docker GUI smoke accepts a scripted proposal and asserts the real note (with `ai-created` + session tags) exists in the collection. The session ledger is in-memory per chat session (per-change revert, undo-session, Browser jump via the session tag); persisting it across restarts is deferred to the M3 transcript work. Keyboard review: `Cmd+Enter` accept / `Cmd+Backspace` reject target the focused (else earliest) pending proposal, `Cmd+Up/Down` cycle pending proposals. Auto-accept: creations only, per-session cap, pause notice when the cap trips, then manual proposals.
15. **Display name — resolved 2026-07-02: "Chat With Your Cards".** Rationale: AnkiWeb search is primitive substring matching, and an add-on has no marketing muscle — the title *is* the marketing, and a descriptive one transmits the value proposition in a single utterance. Keep "AI" in the AnkiWeb subtitle for search. Avoid "Copilot" (trademark) and model-vendor names (backend-plural by design). Repo renamed to `chat-with-your-cards`. Earlier brand-style candidates (Deskmate, Marginalia, Sidekick, Socratic) and the "Chat With Your Collection" variant kept for the record but rejected. SVG icon designed later; it need not derive from the name. **Display-name plumbing (2026-07-06):** Anki's Add-ons pane shows `meta.json`'s `name` key, falling back to the module/folder name when it is absent — it does *not* read `manifest.json` at display time. A packaged `.ankiaddon` install copies `manifest.json`'s `name` into `meta.json` automatically, but a **dev symlink install** (addons21 → this repo) never runs that step, so its `meta.json` (gitignored, Anki-managed) needs `"name": "Chat With Your Cards"` added by hand or it displays `chat_with_your_cards`. Both `manifest.json` and the local `meta.json` now carry the name.
16. **Dogfooding polish (2026-07-06, from real use).** Ten fixes: (1) the CLI stream parser now inserts a paragraph break between consecutive text blocks/messages in a turn (`_text_separator` in `claude_cli.py`), so two assistant messages with no tool call between them no longer glue as "…style.Let me…"; a leading break in a fresh UI bubble is trimmed by the renderer, so it is harmless after a tool chip. (2) Tool chips are collapsible: the header row toggles an expandable detail area showing the tool's input args and result (`SUMMARY_CHARS`/`RESULT_CHARS` raised to feed it; the collapsed row still shows only a short hint). (3) "Suggest change" + send now sets the revised-away proposal aside as **superseded** (restorable) via the new `ProposalManager.supersede` + `proposal_supersede` bridge message, instead of leaving it dangling as pending. (4) Optional `restore_last_chat` config (default off) reopens the last chat on launch. (5) The dock is docked-only (no `DockWidgetFloatable`; the detach button and `toggle_float`/`dock_state` plumbing are gone). (6) The permission-mode chip and open-in-Claude-Code button are now caret dropdowns (pick a mode directly / choose Desktop-app vs Terminal). (7) Claude-Code-style layout: permission-mode + model/effort + Pins moved out of the top header into a control row inside the composer. (8) The Tools-menu entry became a labeled "Chat With Your Cards" submenu ("Open / focus chat", "New chat", each with its shortcut) instead of a bare add-on-title checkbox whose show/hide toggle diverged from the Ctrl+J focus chord. (9) The dock now has a `MIN_DOCK_WIDTH` (320px) floor with `setMinimumWidth` + a load-time clamp — the persisted width had let an accidental drag reopen the panel as an unusable ~70px sliver; the composer control row also gained `flex-wrap` so long labels wrap instead of overflowing at narrow widths. (10) "Open in Claude Code" is now a prominent accent-styled **split button**: the main part opens with the current target (shown in the label — Terminal / Desktop), the caret picks it, and the choice persists (`open_in_claude_target` config). The terminal handoff honors a new `terminal_app` config (empty = Apple Terminal via AppleScript; any other app name launches a temp `.command` via `open -a`, covering iTerm/Warp/Ghostty/…).

## 14. Tracked upstream dependencies (re-check when resuming)

> **Instruction to future agents:** when picking this project back up, re-check the items below and **report their current status to the user** before doing related work — they are external limitations we designed around, and some may have been fixed upstream since 2026-07-05, which would let us simplify.

1. **Claude Desktop cannot resume a Claude Code session by id.** The desktop app supports `claude://code/new?folder=…&q=…` deep links (open a *new* session in a folder with a pre-filled prompt) but has **no** documented way to open a *specific existing* session by UUID. Because of this, "Open in Claude Code → Desktop app" opens a fresh session in `agent-home` with a prompt that tells the GUI agent to read this chat's transcript JSON for context, rather than truly continuing the session. The Terminal path uses real `claude --resume <id>` and has no such limitation.
   - Watch these issues; if any ships a `claude://…/resume?session=<uuid>` (or a `--desktop`/deep-link resume flag), replace the transcript-handoff hack in `_open_in_claude_code(target="gui")` with a direct resume deep link:
     - https://github.com/anthropics/claude-code/issues/50345 (open Desktop to a specific session UUID)
     - https://github.com/anthropics/claude-code/issues/52743 (`--desktop` flag to launch/continue sessions in Desktop)
     - https://github.com/anthropics/claude-code/issues/50067 (add `/resume` to the desktop app)
     - https://github.com/anthropics/claude-code/issues/43943 (let `-p` sessions appear in the Desktop sidebar)
   - Docs to re-verify: https://code.claude.com/docs/en/deep-links and https://support.claude.com/en/articles/14729294-open-claude-desktop-with-a-link (current scheme is `claude://code/new` with `q`, `folder`, `file`).
2. **Pi has no built-in MCP.** The Pi adapter (deferred) will need a pi TypeScript extension bridging its tool calls to our MCP HTTP endpoint. Re-check whether Pi has added native MCP support before writing that bridge.
3. **Codex local install was broken** during the 2026-07-05 probe (volta package missing the platform vendor binary → spawn ENOENT). Re-check before building the Codex adapter; also a live test case for the doctor panel.

## 15. Learning from the user's edits (skill reflection loop)

The agent gets better by watching how the user changes AI-written cards
and folding confirmed patterns back into the card-authoring skill. The
loop is capture -> batch -> reflect -> confirm; nothing changes agent
behavior without an explicit user accept.

- **Capture, channel 1 (accept-time diffs).** When a proposal is accepted,
  the diff between what the agent proposed and what the user accepted
  (fields, tags, deck, declined field changes) is recorded as a `reviewed`
  observation. Verbatim accepts record nothing.
- **Capture, channel 2 (snapshot diffs).** Every content write the system
  makes stores a per-note snapshot (the last state the system left the
  note in) in `user_files/learning/snapshots.json`. On dock open, a scan
  compares tracked notes against their snapshots: field/tag changes become
  `edited_later` observations, deletions become `deleted_later` (the
  strongest rejection signal). Because it is snapshot-based rather than
  hook-based, it catches edits made in the Browser, the editor, or on
  AnkiDroid/AnkiMobile after a sync. The scan is one bulk `notes.mod`
  query; full note reads happen only for changed notes. Same-second edits
  can hide behind the 1s `mod` granularity (accepted; a later edit or any
  other change re-exposes them). Reverts emit a `resync` (refresh tracked
  snapshots only) so system writes are never mistaken for user edits.
- **No cap, by design (decision 2026-07-05).** One snapshot per AI-touched
  note means the store is a constant factor of the AI-touched slice of the
  collection itself (~1 KB/note); a cap (and its config + resource-meter
  UI) would solve a problem that cannot occur. The doctor panel reports
  snapshots / pending observations / bytes.
- **Batch + nudge.** Observations accumulate in
  `user_files/learning/observations.jsonl` (append-only, replayed on
  load; consumption is an event). A footer nudge appears at
  `learning_nudge_threshold` pending observations (default 10) or when
  any observation is older than `learning_nudge_days` (default 7).
  Dismissing hides it until MORE observations accumulate. The nudge states
  explicitly that reviewing starts a new chat and the current one stays in
  History.
- **Reflection chat.** Clicking the nudge starts a new chat seeded with a
  visible kickoff message (pushed as a `user_message` event, since the
  webview normally renders user bubbles itself). The agent calls
  `get_edit_observations` (pending observations + current skill), reasons
  about patterns per the `skill-maintenance` meta-skill, and calls
  `propose_skill_update` with a summary, plain-language pattern
  statements, and the full revised skill markdown.
- **Meta-skill, not hardcoded structure (decision 2026-07-05).** HOW the
  skill gets updated is itself a user-editable skill
  (`skill-maintenance/SKILL.md`). The default instructs arbitrary inline
  edits - sharpen/merge/delete bullets where they belong, no separate
  "learned preferences" section; users who want a different organization
  edit the meta-skill, not the code.
- **Always gated.** `skill_update` proposals require explicit user
  confirmation in EVERY permission mode (including trusted-writes) and are
  excluded from bulk accept-all: a skill edit changes all future agent
  behavior. The card shows the pattern statements and a unified diff of
  the skill. Accepting writes the skill, archives the prior version to
  `user_files/learning/skill-backups/`, and consumes the observations it
  was based on; there is no ledger revert (restore = copy the archive
  back).
- Deterministic coverage: `tests/test_learning.py` + observe/skill-update
  tests in `tests/test_proposals.py` (fake collection, field-compare scan
  path) and a real-collection GUI-smoke check (bulk-mod scan path, real
  SKILL.md write/archive/consume). Only pattern *quality* is manual QA.

## 16. Deck management and filtered decks (2026-07-06)

The agent can manage deck structure and study configuration, not just
note content: create/rename decks, change deck options, move cards
(already a bulk op), and create/reconfigure/rebuild/empty filtered
decks. Every operation is a `deck_op` proposal — one confirmation card
with plain-language sample lines — flowing through the same
`_finish_submission` machinery as the bulk ops (direct apply under
trusted-writes within the write budget; deck ops cost 1 budget unit).

- **Tools.** Read: `get_deck_info` (card count, subdecks, options preset
  with full config + how many decks share it, or filtered terms).
  Write: `create_deck`, `rename_deck`, `set_deck_options`,
  `create_filtered_deck`, `update_filtered_deck`,
  `filtered_deck_action` (rebuild | empty).
- **Options are dot paths into the real preset dict** (`new.perDay`,
  `rev.perDay`, ...), validated against the CURRENT config: only
  existing keys with type-compatible values are accepted, so a typo
  cannot plant a garbage key Anki silently ignores. Presets are shared
  objects; the proposal card warns "shared by N decks" (counted via
  `col.decks.all()`), and `set_deck_options` is the one deck op that
  takes a backup checkpoint before applying.
- **Filtered decks** use the stable legacy surface: `decks.new_filtered`
  + `deck["terms"] = [[search, limit, order]]` (1-2 terms, order codes
  0-8) + `decks.save` + `sched.rebuild_filtered_deck`. Term searches are
  validated with `find_cards` at submit time and the card shows an
  approximate match count (approximate because gather excludes suspended
  cards and cards already in another filtered deck). The gathered count
  is reported on the resolved card.
- **Reverts.** create_deck → remove (REFUSED if the deck acquired cards
  or subdecks); rename_deck → rename back, children follow (looked up by
  the NEW name, never by stored id — legacy `decks.get(did)` falls back
  to the Default deck for a missing id, and a revert must never touch
  the wrong deck); set_deck_options → restore prior values;
  create_filtered_deck → remove the deck (Anki returns its cards home
  with scheduling intact); update_filtered_deck → restore prior
  terms/resched and rebuild. Rebuild/empty are NOT ledger-revertible
  (nothing restorable is stored) and create no ledger entry.
- **UI plumbing.** deck_op cards render through the bulk-card body
  (badge "Deck change", accept button "Apply"); after any deck op
  applies or reverts, an injected `after_deck_change` callback refreshes
  the deck browser / overview, since deck ops touch no note ids and the
  reviewer-refresh path never fires for them.
- Renaming a deck via `deck["name"] = new; col.decks.save(deck)` renames
  child decks in the Rust backend; the Docker smoke asserts this against
  a real subdeck (plus the full filtered lifecycle and both revert
  refusal paths).

## 17. LLM-graded long-recall cards + capstone gating (2026-07-06)

Design captured from a brainstorming pass. Status: **agreed direction, not
yet built.** The Layer-1 server component may end up living outside this
repo; the agent-driven Layer-2 parts belong to CWYC.

### 17.1 Components: always-on server + desktop add-on

The graded card must reach an LLM from **any** device, and the user's
laptop is not always on — so grading runs on an **always-on HTTP server**,
hosted inside the user's **VPN**, NOT in the desktop add-on. Only the
progress bubble (§17.8) is desktop-first; everything else is
cross-platform.

- **Always-on server (in the VPN).** The grader / interactive-tutor
  endpoint. The big card's **template JS** POSTs the recall answer,
  renders the back-and-forth, and shows feedback — works on AnkiMobile /
  AnkiDroid / desktop because it talks to the server, not the laptop. The
  server records per-atomic verdicts **and full conversations** (§17.9).
- **Chat With Your Cards add-on (desktop).** The single *writer* for
  scheduling: reads the server's grading events and applies the
  consequences to the collection — fail missed atomics, gate the
  capstone, promote related cards (§17.5–17.7). Also builds/maintains the
  dependency graph and renders the desktop bubble. Deliberately **not**
  the grader (the laptop isn't always on).
- **Reconciler add-on.** Consistency checks (§17.3, §17.7).

A "big card" is a *capstone / integration test* over a set of atomic
cards. Net split: **the server grades (all platforms) and records; the
desktop add-on writes scheduling and shows the bubble.**

**VPN — what it does and doesn't buy.** Solves **auth** (the VPN is the
boundary; no public endpoint, no shared secret). **CORS** becomes a
one-line permissive header (webview-origin policy, network-independent).
Does **not** solve **offline** — a card off-VPN / offline still can't
reach the grader, so it must degrade gracefully. Still needs a **TLS**
story (Anki mobile webviews resist plaintext even to a private host; e.g.
Tailscale certs).

### 17.2 First card (the proving ground)

"List the members of a fixed canonical sequence, in order." Chosen because it dodges
both hard problems: the atomic cards **already exist** in such a deck
("given a member, what comes after it"), the composition is a *declared*
fixed set (no graph discovery, never goes stale), and every mechanism it
needs is FSRS-native. Build this before anything semantic.

### 17.3 Graphs: two kinds, very different difficulty

- **Authored composition** — the big card *is* a known fixed set of
  atomics. Declared, not discovered; the agent writes the dependency set
  on the user's instruction ("I have these atomic cards, wire them up").
  Never drifts. This is the easy, high-value majority case.
- **Discovered semantic** (e.g. a linear-algebra topic) — the agent
  sweeps the collection for related cards. Kept cheap by **incremental
  sweeps**: track the last-sweep datetime and only scan cards added
  since. Run as a desktop batch a few days before the big card is due;
  update the big card's stored reference set as needed. Believed
  tractable and inexpensive per the user's experience with search agents.

The big card **stores references to its related cards by both `nid` and
`guid`** — `nid` for fast local lookup (survives retag/move), `guid` to
survive export/re-import across collections. A periodic reconciler add-on
walks the reference set (a handful to a few dozen cards) and notifies the
user on inconsistency: missing / deleted / edited / newly-relevant notes.
Per-card config decides which of those conditions merely warn vs. trigger
a user review. Explicit goal: **most cards need no manual intervention;**
the review path is the exception, not the norm. If the big card *itself*
is edited, that can invalidate and rebuild the graph.

**One relevant-cards set; usage is the LLM's call.** A big card hangs off
a single set of **relevant cards** (the graph). There is *no* structural
split between "cards to test" and "cards for hints" — the LLM decides, per
card per session, how to use each: test it (→ per-card verdict → maybe
fail it), draw a hint from it, or both. The set spans note types and
concepts (the "what comes next" adjacency cards *and* a "Pastorals"
concept card), and a single recall miss can fail any subset of them —
e.g. an adjacency card *and* the Pastorals card, if the user genuinely
can't recall those books. So the "enumerate the New Testament books" card
is not defined solely by its adjacency cards. The capstone's gate (§17.5)
is defined over this one set (or over whatever it has recently failed) —
not over a pre-declared sub-partition.

### 17.4 Config in a card field

Per-card behaviour (gate rule, timing lead-time, missing-note handling,
etc.) lives as **human-readable YAML/JSON in a card field**, written and
maintained by the CWYC agent (not hand-edited by the user, though
readable). Deliberately *not* base64/opaque-encoded: human-readability
wins; the `{{ }}` template-collision risk is minor and avoidable without
sacrificing legibility.

### 17.5 Gating the capstone

The capstone is **suspended/unsuspended** based on atomic state — an
availability control, not interval math, so FSRS never sees a rewritten
interval. The rule is per-card configurable. Default candidate: show the
capstone when **nothing in its set is currently lapsed/relearning**
(softer than "all mature", which would rarely fire with ~38 atomics).
This is the knob that decides whether the capstone resurfaces monthly vs.
never.

### 17.6 Rescheduling: mostly FSRS-native, one thing to avoid

1. **Fail the atomics the LLM judged wrong** = record honest **Again**
   ratings on those cards. FSRS-legit (the exact button the user would
   press). Applied via the collection API with Anki closed (the same
   lib-with-Anki-closed primitive used for filtered decks).
2. **Short-interval re-drilling** = *free.* An Again drops the card into
   FSRS relearning steps automatically — that IS "practice a few times at
   short intervals, then graduate." No custom interval logic.
3. **Cross-card promotion** = a **filtered deck with "Reschedule cards
   based on my answers" ON.** The related cards surface for *real* review
   and FSRS updates them from genuine ratings — this is FSRS-native, not a
   fight. Reuses the filtered-deck machinery in §16. (Minor caveat:
   early-review distortion, which FSRS handles gracefully.)
4. **Avoid:** blind `setDueDate` surgery to move cards earlier without a
   review — that lies to the scheduler. Rescheduling-OFF cram is only for
   "don't disturb my schedule" exam prep, which is not this use case.

### 17.7 Single-writer discipline & the sync facts

Grading happens on the always-on server from any device, but **all
collection writes happen in one place: the desktop add-on** (batch, or
next desktop open, via the collection API). The server is the
record/brain; the add-on is the single writer. Mobile never writes
scheduling directly — it grades via the server, and the add-on later
applies the consequences.

**Correction to an earlier draft:** Anki does **not** force a one-way /
full sync when you review on two devices before syncing. Per the manual,
"reviews and note edits can be merged, so if you review or edit on two
different devices before syncing, Anki will preserve your changes from
both locations," and if the same card was reviewed in two places "both
reviews will be marked in the revision history, and the card will be kept
in the state it was when it was most recently answered." A one-way sync
is triggered only by **structural / schema changes** (adding a field,
removing a card template, …), never by reviews. So the "fail on one
device, good on another before syncing" case is benign last-writer-wins
on that single card, with both reviews logged — no data loss, no forced
full sync.

Residual discipline that still matters: the apply path is **keyed to a
grading-event id + collection revision** and **idempotent** (a re-run
must never double-fail a card). The reconciler add-on surfaces any
divergence between the server's recorded events, the add-on's applied
state, and the actual collection.

### 17.8 Progress bubble — desktop-only, add-on-rendered

Decision: render the bubble **only on desktop, injected by the add-on**
into the reviewer (CWYC already hooks the reviewer webview). No card-
template JS, no ping, no server. Because the add-on has full collection
access, the count is **precise and always up to date** (modulo a pending
sync from another device) — and it can show true **maturity** ("17/20
mature"), not merely "seen." (The earlier seen-vs-mature limitation was
purely the *webview* being blind to scheduling; an add-on is not, so
desktop-only unlocks the better metric for free.) Mobile shows no bubble
for now; revisit only if the feature earns it (§17.1 deferred bucket).

### 17.9 Grading contract & the tutor loop

Developed **by examples**, not spec-first, and expected to carry **bespoke
per-card logic.** The interaction is an escalating-hint tutor loop, not a
single grade:

1. User does one long active-recall pass (typed or voice-transcribed,
   possibly self-correcting: "actually, earlier I meant…").
2. The LLM either grades immediately or gives a **hint** about what was
   forgotten/wrong ("you got some of the first five out of order — try
   again?").
3. User retries; hints get **progressively easier** each round until the
   user succeeds or gives up.
4. Hints can be **derived from the collection graph** — e.g. "you forgot
   three books" → user still fails → "those three are all the Pastorals",
   generated by noticing the user *has* a Pastorals card. The hint
   generator reads the relevant-cards set (§17.3), not just the answer.
5. Hints can also be **generic** ("the book starts with F___"). The tutor
   should just be as good a tutor as possible, mixing graph-derived and
   generic hints.

The judge decomposes the whole session into **per-atomic verdicts**. A
reviewable UI reveals each verdict and lets the user override unfair ones
— **load-bearing**, because mapping a ramble to "which adjacencies
failed" is ambiguous (a single transposition looks like two broken
adjacencies). Seed the contract with real transcripts including a
self-correction and a transposition.

**Capture everything.** The server records full conversations, not just
verdicts, as a corpus for improving the tutor over time (how exactly —
eval set, few-shot exemplars, fine-tuning — is open; committing to the
capture now is the point).

**Drafted contract:** see [`GRADING_CONTRACT.md`](GRADING_CONTRACT.md) —
the server API (provision / session / turn / finalize), the verdict
schema (with a graded `grade_map`: hint depth → FSRS rating), the
escalating-hint ladder, and three worked transcripts (clean pass;
self-correction + transposition + disputed verdict; multi-turn hints with
give-up).

### 17.10 Operational timing

**One nightly desktop sweep** processes big cards due within their
**per-card lead-time** window. Per-card lead time: yes (cheap, no
inefficiency). Per-card independent cron/frequency: no (over-engineering
for no gain).

### 17.11 Open questions

- Grading-contract schema and per-card bespoke logic — fall out of the
  seed transcripts.
- Exact gate-policy thresholds — empirical, tune by use.
- Precise keying/idempotency of the apply path and the reconciler's
  divergence UX.
- **Server deployment in the VPN** — approach known, mostly execution:
  TLS via Tailscale MagicDNS certs (or Caddy + a domain); CORS is a
  one-line header. Offline is *not* a design item — SotA grading needs
  the network, so a card that can't reach the grader just shows a
  graceful "can't grade offline" message.
- How the **relevant-cards set** (§17.3) is declared/maintained per card,
  and how the gate is defined over it (whole set vs. recently-failed
  subset).
- How captured conversations are actually used to improve the tutor
  (eval set / exemplars / fine-tuning).

**Deferred (out of near-term scope):** a **mobile** progress bubble
(desktop-only for now, §17.8). The cross-platform server itself is core,
not deferred.
