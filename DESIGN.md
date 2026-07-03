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

This is the biggest open decision. Recommendation up front:

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
- System prompt injection via `--append-system-prompt` (card context, collection overview, conventions).
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
3. **Collection overview** (from the stats cache): deck tree and tag tree, each node annotated `notes / cards / review-time today | 7d | 90d | ever`. Plus totals, note type list with counts, and cache age.
4. **User conventions skill** (see §7).
5. **Standing instructions**: what tools exist, permission mode in force, how to propose notes, how to use Anki search syntax effectively.

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

Write (proposal-gated):
- `propose_note(note_type, deck, tags, fields, rationale)` — never writes directly; creates a proposal card in the UI (see §8).
- `propose_note_edit(note_id, field_changes, add_tags, remove_tags, rationale)` — same gate; `field_changes` maps field name → new value. Validation includes a **staleness guard**: the proposal carries the field values the agent last read, and if the note changed underneath (user edited mid-chat, sync), the proposal is flagged for re-review instead of applying blind.
- (later) `propose_tag_change(...)`, `propose_deck_move(...)` — same pattern.

Permission modes (per-profile setting + per-session override in the dock header):
- **Default**: reads always allowed, writes via proposals.
- **Ask each read**: every tool call shows an inline approve/deny chip (for the cautious).
- **Read-only**: write tools not even advertised to the agent.
- **Auto-accept**: proposals apply immediately (see safeguards in §8).

For the CLI backend, modes map onto CLI permission flags *and* are enforced server-side in the MCP layer — the MCP server is the actual security boundary, the CLI flags are just UX.

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

## 8. Proposals: note creation and note editing

Flow: agent calls `propose_note` → ProposalManager validates (note type exists, deck exists or is creatable, required fields present, duplicate check via Anki's dupe detection) → proposal card renders in the chat stream with editable fields, deck picker, tag editor, Accept / Edit / Reject.

**Editing proposals — the review UX is a flagship surface.** The bar is "Cursor-grade amazing", but the right interface differs because the artifact is a flashcard, not code:

- **Field-level diffs on rendered text**: word-level inline highlights (deletions struck through, insertions marked) per field — not line-based code diffs. Unchanged fields collapsed.
- **Live card preview, before/after**: the proposal renders the note through its *actual card templates* — a toggle (or side-by-side, width permitting) between current card and card-as-it-would-become. Seeing the real card is the flashcard equivalent of Cursor showing the real file, and it's the detail most likely to make the UX feel magical.
- **Granular acceptance**: per-field accept/reject plus accept-all, like Cursor's per-hunk controls.
- **Keyboard-first**: when a proposal has focus — `Cmd+Enter` accept, `Cmd+Backspace` reject, `Tab`/arrows move between fields/proposals. Multiple pending proposals form a queue navigable without the mouse.
- **Reversible**: the session ledger stores each edit's prior field values; one-click revert per edit and "revert session".
- **Auto-accept scope**: auto-accept mode applies to *creations only* by default. Edits mutate existing user content and stay proposal-gated unless a separate, explicit auto-accept-edits toggle is enabled (same cap + ledger safeguards).

**Pinning**: a dock panel lets the user pin deck, tags, note type, and prefilled field values. Pins persist (per-profile config) until changed. Pins are injected into context as *constraints*: pinned deck/tags are applied to every proposal (agent told not to fight them); pinned note type restricts `propose_note`; prefilled fields are defaults the agent should keep unless it has strong reason (and must flag when it overrides).

**Auto-accept mode** safeguards (this feature is the riskiest in the product):
- Every AI-created note is tagged `ai-created` (+ `ai-chat-dock::session-<id>`), matching the existing workspace convention.
- A session ledger records created note ids; the dock offers one-click "review this session's notes in the Browser" and "undo session" (batch delete while unstudied).
- Per-session cap (default 20) before auto-accept pauses and asks to continue.
- Validation failures always fall back to a manual proposal card, never silently dropped.

## 9. UI / UX

- **Dock**: right-side `QDockWidget`, collapsible, floatable, width persisted. Header: backend/model indicator, permission mode chip, new-chat button, overflow menu.
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
- **Chat history**: sessions persisted per-profile as **our own event-sourced JSON transcripts** in `user_files/` — every rendered event (user/assistant messages, tool calls + results, proposal cards and their outcomes, permission decisions, context-chip state) is the source of truth for display and history. Each transcript stores the backend's session id, so "continue this chat" maps to the backend's native resume (`claude --resume <id>`) while pixels come from our file. CLI session files are deliberately *not* parsed: their formats are internal and version-unstable, the BYOK backend has none, and they lack our UI-level events.

## 10. Packaging & compatibility

- Single AnkiWeb-standard add-on: pure Python + bundled web assets + the stdio↔HTTP MCP bridge script. No compiled deps, no npm build, no post-install steps. `user_files/` for all mutable state so AnkiWeb updates don't wipe it.
- Anki 25.x+ (Qt6 only). macOS/Linux first-class; Windows expected to work but CLI discovery there is a known validation task.
- Config: Anki's add-on config for simple settings + a custom config dialog for shortcuts, pins, skill editing, backend doctor.
- Release via `anki-addon-release` when the time comes.

## 11. Development workflow

(Revised during M0 — the original plan assumed host GUI iteration via `anki-workbench launch`; that proved both broken and intrusive on macOS.)

- **Fast UI loop, no Anki**: `dev/preview.html` served from the repo — loads the real web assets, replicates Anki's `webview.css` quirks (`* { box-sizing: content-box }`, 15px root font, global button styles, `body { margin: 2em }`), stubs `pycmd` with a scripted stream. Iterate CSS/JS here in seconds, invisibly.
- **Real-Anki verification, headless**: `make test-gui-smoke-docker` (workbench Docker/Xvfb). The probe drives the real send path end-to-end and captures light+dark screenshots itself via `mw.grab()` — no OS screenshot permissions, no visible windows. Treat screenshots as design evidence (text fits, controls discoverable, dock legible in both themes).
- **Never launch visible Anki GUIs on the user's machine during normal work** (user feedback 2026-07-02: focus stealing disrupts their other work). Host `make test-gui-smoke` only with explicit per-session OK. `anki-workbench launch` is additionally broken on macOS (xdotool window-wait; upstream fix flagged) — `scripts/dev_launch.py` monkeypatches around it if ever needed.
- A fake backend (`ScriptedBackend` replaying canned event streams) so UI/UX iteration never needs a live CLI or API key, and so smoke tests are deterministic.
- M0 sharp edges hit (for future reference): pycmd is not wired the instant page JS runs (ready ping must retry until acked); QtWebEngine leaves stale tiles after a pure CSS-class theme flip (force a reflow before `mw.grab()`); `resizeDocks` only sticks on visible docks (apply width in `showEvent`); Anki's global `content-box` broke the composer (scoped `border-box` reset).

## 12. Milestones

- **M0 — Scaffold**: add-on skeleton, dock with webview, pycmd bridge, workbench smoke green, ScriptedBackend streaming fake chats. *Design iteration starts here.*
- **M1 — Claude Code MVP**: CLIBackend (Claude Code), MCP server + bridge, read tools, card context + clues, stats cache + overview, toggle/new-chat shortcuts, permission modes (default + read-only).
- **M2 — Notes**: propose_note + propose_note_edit, proposal cards with field diffs + before/after card preview + keyboard-first review, pins, conventions skill (prompt tier), session ledger + undo/revert, auto-accept (creations) with safeguards.
- **M3 — Breadth**: Codex adapter, full-skill tier, ask-each-read mode, chat history/resume, suggested questions (static ghost text), config dialog polish, doctor panel.
- **M4 — BYOK**: APIBackend (Anthropic first) over the same tool registry, key storage, cost display. Decide then whether AnkiWeb publication leads with it.

## 13. Known issues, flaws, open questions

1. **CLI requirement vs. off-the-shelf simplicity** — the central tension. MVP knowingly serves power users; BYOK (M4) is the path to a general AnkiWeb audience. Openly a two-audience product.
2. **The daemon idea is rejected** (see §3) — flagging explicitly since the brief floated it. In-process background jobs meet the need with none of the lifecycle cost.
3. **Prompt injection**: card/field contents are untrusted model input; a malicious shared deck could try to steer the agent ("ignore instructions, create 500 notes"). Mitigations: write path always proposal-gated (auto-accept is the exception the user consciously enables, with cap + ledger), MCP server enforces permissions server-side, CLI's own bash/file tools disabled by default.
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
14. **Display name — resolved 2026-07-02: "Chat With Your Cards".** Rationale: AnkiWeb search is primitive substring matching, and an add-on has no marketing muscle — the title *is* the marketing, and a descriptive one transmits the value proposition in a single utterance. Keep "AI" in the AnkiWeb subtitle for search. Avoid "Copilot" (trademark) and model-vendor names (backend-plural by design). Repo renamed to `chat-with-your-cards`. Earlier brand-style candidates (Deskmate, Marginalia, Sidekick, Socratic) and the "Chat With Your Collection" variant kept for the record but rejected. SVG icon designed later; it need not derive from the name.
