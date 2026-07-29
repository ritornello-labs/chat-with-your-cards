# Chat With Your Cards — configuration

- `toggle_shortcut` (default `Ctrl+J`, shown as Cmd+J on macOS): context-aware chord
  that cycles the dock. If the dock is collapsed to its side rail, expands it and
  focuses the message box. If focus is in the chat, collapses the dock back to the
  rail and returns focus to the reviewer/deck browser. Otherwise focuses the chat.
- `new_chat_shortcut` (default `Ctrl+Shift+J`): starts a fresh chat with fresh
  context; focus stays in the message box.
- `defer_shortcut` (default `Ctrl+Shift+D`): while reviewing, push the card on
  screen to later in this session — "not this one right now". It is NOT a bury:
  the card keeps its due date, interval and queue, so it stays due today and a
  device without this add-on is unaffected. Also on the Tools menu and the
  reviewer's right-click menu, and available to the assistant as `defer_card`.
  "Bring back a deferred card" (Tools) makes one the NEXT card. The deferral
  itself is stored on the card and syncs; the "show it next" part is
  session-only and is forgotten when Anki closes.
- `dock_width` (default `420`): expanded dock width in pixels. The width you drag
  the dock to is remembered automatically when the profile closes.
- `dock_collapsed` (default `true`): whether the dock is collapsed to its slim
  side rail. Managed automatically (remembers how you left it).
- `dock_side` (default `right`): which window edge the dock lives on (`left` or
  `right`). Also switchable from the dock's Settings panel (gear icon).
- `vim_mode` (default `false`): vim keybindings in the message box (modal
  editing; Esc/`fd` for normal mode, Enter still sends, Shift+Enter for a
  newline). Also toggleable from the Settings panel.
- `vim_mappings` (default `[]` = stock vim keys): your personal vim mappings
  as `[keys, mapped-to, mode]` triples with vim `:map` semantics; `mode` is
  `normal`, `insert`, or `visual`. Example — leave insert mode with `fd`, and
  make `j`/`k`/`0`/`$` move by *visual* (screen) line inside a wrapped message
  instead of the whole logical line:
  ```json
  [["fd", "<Esc>", "insert"],
   ["j", "gj", "normal"], ["k", "gk", "normal"],
   ["0", "g0", "normal"], ["0", "g0", "visual"],
   ["$", "g$", "normal"], ["$", "g$", "visual"]]
  ```
  All four motions (`gj`/`gk`/`g0`/`g$`) are genuinely visual-line-aware in the
  bundled vim engine (verified 2026-07-14 against wrapped lines), and mapping
  `0` does **not** break numeric counts — `10j` still moves ten lines, because
  the count parser treats a `0` mid-count as a digit, not the mapped motion.
- `theme` (default `teal`): the dock's colour palette, also selectable from the
  Settings panel. One of `teal` (cool porcelain + deep teal), `indigo` (neutral
  paper + muted indigo), or `evergreen` (oat paper + deep pine). Each has a
  light and a night-mode variant; inside Anki the app follows Anki's night-mode
  state, standalone it follows the OS.
- `backend` (default `auto`): `auto` uses the Claude Code CLI when installed and
  falls back to a built-in demo backend; `claude` requires the CLI; `scripted`
  forces the demo backend.
- `claude_cli_path` (default empty): explicit path to the `claude` binary when it
  is not on the standard lookup paths.
- `model` (default empty = the CLI's own default): which model the Claude Code
  backend uses. Accepts an alias (`fable`, `opus`, `sonnet`, `haiku`) or a full
  model id. Also selectable live from the dock header (Model chip).
- `effort` (default empty = the CLI's own default): reasoning effort level, one of
  `low`, `medium`, `high`, `xhigh`, `max`. Also selectable from the header.
  Switching model or effort mid-chat keeps the current conversation - the CLI is
  re-invoked with `--resume` on your next message. Your choice is remembered
  across chats and restarts; the CLI default only applies until you first pick.
- `fast_mode` (default `false`): headless "fast mode" for faster Opus output.
  Requires the Claude Code CLI `2.1.205` or later; only takes effect on Opus
  (harmless, but a no-op, on other models). There is no live mid-session
  toggle upstream - like a model/effort switch, turning it on or off applies
  from your next message (the CLI process respawns with `--resume`). Also
  toggleable from the dock's Model/effort picker.
- `agent_tools` (default `"sandbox"`): the agent's environment-access tier — an
  axis orthogonal to `permission_mode` (which gates *collection* writes). This
  gates the agent's own shell and file tools and how their calls are approved in
  the add-on's headless CLI session. Selectable live from the dock's "Agent
  tools" chip; switching respawns the CLI with `--resume` on your next message,
  like a model switch. In every non-sandbox tier Claude Code's built-in circuit
  breaker still blocks `rm -rf /` and `rm -rf ~`.
  - `"sandbox"` (default): the CLI runs with `Bash`, `Edit`, `Write`, and
    `NotebookEdit` disabled (`Read` stays read-only). The agent lives inside
    your collection; it cannot run commands or write files on your machine.
    This is stricter than any of Claude Code's own permission modes.
  - `"acceptEdits"`: leaves those tools on and runs with
    `--permission-mode acceptEdits` — file edits and commands auto-approve.
  - `"auto"`: tools on with `--permission-mode auto` — a safety classifier vets
    each call and blocks risky ones (a block is returned to the agent as a failed
    tool result, which it then explains in chat; nothing silently drops, and the
    headless session never pauses for a human — there is no interactive gate).
    Needs a premium model: the CLI silently downgrades `auto` to a no-classifier
    mode on Haiku, so the dock disables `auto` when Haiku is selected and falls
    back to `sandbox` if you switch to Haiku while it is on.
  - `"full"`: tools on with `--permission-mode bypassPermissions` — every check
    bypassed (auto-approve, no per-command prompt).
  - **The catch (all non-sandbox tiers):** the agent reads your card content,
    and card content is untrusted — a shared or downloaded deck can contain text
    crafted to steer the agent into running a command, and on these tiers that
    command can run on your computer immediately. Only leave sandbox on
    collections you trust. Anki card changes still go through the review flow by
    default (prefer the built-in propose tools; a shell should use AnkiConnect,
    never write the `.anki2` file directly), but a shell can bypass that.
  - Not offered: `plan` (it makes the agent refuse to write, which would stop
    card proposals — use the `read-only` *permission mode* for that) and
    `manual`/`dontAsk` (headless, they collapse to either auto-run or auto-deny
    and add no distinct guarantee over the tiers above). Verified against Claude
    Code CLI 2.1.208.
- `suggested_questions` (default `true`): show a context-aware suggested
  question as gray ghost text in the empty composer; Tab accepts it, typing
  dismisses it.
- `restore_last_chat` (default `false`): when `true`, reopening Anki restores
  your most recent chat in the dock (replayed from its saved transcript, with
  the agent resumed) instead of starting empty. Older chats remain in History
  either way.
- `open_in_claude_target` (default `"terminal"`): the default target the "Open
  in Claude Code" split button acts on — `"terminal"` or `"desktop"` (the
  desktop app; `"gui"` is accepted as a legacy alias for `"desktop"`).
  Changing it from the button's dropdown persists here.
- `terminal_app` (default `""` = Apple Terminal): which macOS terminal the
  terminal handoff opens. Empty uses Terminal.app via AppleScript; any other
  app name (e.g. `"iTerm"`, `"Warp"`, `"Ghostty"`) is launched with a temporary
  `.command` script via `open -a`.
- `source_fields` (default `{}` = look everywhere): optional per-note-type
  restriction of which fields may contain card sources, e.g.
  `{"0 Cloze": ["Extra"]}`. By default any URI in any field is treated as a
  potential source the assistant may open (web via fetch, local PDFs via
  read).
- `web_access` (default `true`): allow the assistant to use web search and
  page fetching (useful for sourcing card content). Set `false` to keep the
  agent strictly inside your collection.
- `mcp_servers` / `mcp_inherit_user` / `mcp_disabled` — **advanced, opt-in MCP
  widening.** The Settings panel (gear) has an "MCP tools" section: a toggle for
  `mcp_inherit_user` and an "Edit servers…" button that opens this config for
  the `mcp_servers`/`mcp_disabled` JSON. MCP setup is read when the backend is
  built, so changes take effect **on your next new chat** (not mid-conversation).
  By default the assistant only ever sees this add-on's own `anki` tools: the CLI is
  launched with `--strict-mcp-config` and a config containing just that one
  server, so nothing else you have configured for coding agents is reachable
  from inside a chat about your cards.

  **Why this matters before you widen it:** card and field content is
  untrusted input the same way a web page or email attachment is. The
  assistant reads whatever is in your notes to do its job, so a booby-trapped
  shared deck could embed text aimed at *it*, not you - instructions telling
  it to run a tool, fetch a URL, or exfiltrate data. With the default strict
  scope, the worst it can reach is your own collection. Turn on `mcp_servers`
  or `mcp_inherit_user` and that same prompt-injected text could try to steer
  the assistant into your filesystem, GitHub, browser, or whatever else those
  servers expose. Only widen this if you understand and accept that, and
  ideally only with decks/sources you trust.

  - `mcp_servers` (default `{}`): a dict of extra MCP servers to hand the
    assistant, keyed by name, each value a server spec in the same JSON shape
    Claude Code's own `--mcp-config` / `.mcp.json` uses (e.g.
    `{"type": "http", "url": "...", "headers": {...}}` or a stdio
    `{"command": "...", "args": [...]}`). Merged verbatim alongside the
    built-in `anki` server. A server named `anki` is ignored (logged to the
    backend log) rather than allowed to shadow the built-in one.
  - `mcp_inherit_user` (default `false`): when `true`, drops
    `--strict-mcp-config` so your own Claude Code MCP servers (from your
    global/project Claude Code config) load too, in addition to `anki` and
    anything in `mcp_servers`.
  - `mcp_disabled` (default `[]`): a list of server names (ours or an
    inherited one) to turn back off, e.g. `["github"]` after enabling
    `mcp_inherit_user`. `"anki"` cannot be disabled this way - it is ignored
    (logged) because that would silently break every card proposal.
- `widget_rendering` (default `false`): let the assistant render small
  interactive HTML widgets inline in the chat (`render_widget` tool) — charts,
  diagrams, mini-dashboards. Also in the Settings panel (gear), and offered
  inline the first time the assistant tries while it is off. Widgets run in a
  hard sandbox regardless of this setting: an opaque-origin iframe (no access
  to the chat, this add-on, or your collection) with a CSP that blocks all
  network access — display only. The toggle is about consent and surface
  area, not the security boundary. Applies immediately, mid-chat included.
- `anthropic_api_key` / `openai_api_key` (default empty): paste an API key to
  bill usage to it instead of the agent's own login. Stored in Anki's
  plain-text add-on config - the less-secure option.
- `anthropic_api_key_op` / `openai_api_key_op` (default empty): a 1Password
  reference (`op://Vault/Item/field`) resolved via the `op` CLI when the
  agent starts; the secret never touches disk. Takes precedence over the
  pasted key.
- `permission_mode` (default `default`): `default` allows collection reads without
  asking and gates all writes behind proposal cards; `read-only` removes the
  write tools entirely; `ask-each-read` additionally shows an inline
  Allow/Deny chip for every collection read (for chats about untrusted
  shared decks) - a denial refuses the call, and an unanswered prompt neither
  approves nor refuses it: the call reports back that it did not run, and the
  chip stays answerable until `approval_timeout_minutes`; `auto-accept` applies the assistant's note *creations*
  and native card grading immediately (up to `auto_accept_cap` affected
  notes/cards per session) while edits stay behind proposals;
  `trusted-writes` applies creations, edits, bulk operations, change sets,
  and native grading directly (an Anki backup checkpoint is forced before bulk
  applies) up to `write_budget` notes per session — after that everything
  falls back to manual review. Deleting notes always asks, in every mode.
- `approval_timeout_minutes` (default `5`): in `ask-each-read` mode, how long an
  unanswered Allow/Deny prompt stays answerable. The chip shows the deadline
  counting down; past it the chip reads "Expired, no answer" (never "Denied" -
  you refused nothing) and clicking it no longer resumes anything. This is
  deliberately short: a prompt you have not answered in five minutes is one you
  have moved on from, and without an expiry the assistant kept re-raising
  abandoned requests hours later. Set `0` to disable expiry and let prompts stay
  live indefinitely. Applies to the next prompt raised, no restart needed.

  Note this is *not* how long a tool call waits: a call blocks for at most 45s
  (`approvals.APPROVAL_GRACE_S`, kept under the MCP client's own per-request
  ceiling) and then reports that it did not run. Answering later is still
  useful - an Allow inside the window is picked up and the work resumes.
- `auto_accept_cap` (default `20`): in `auto-accept` mode, how many notes may be
  created and how many cards may be natively graded without review per chat
  session. The two operations keep separate counters, so a card-writing run
  does not silently consume the grading allowance (or vice versa).
- `write_budget` (default `200`): in `trusted-writes` mode, how many notes may
  be written and how many cards may be natively graded directly per chat
  session before pausing for review. These also use separate counters.
- `conventions_prompt` (default empty): your note-authoring conventions (style,
  field usage, tagging). Injected into the assistant's instructions for every
  proposal and materialized as `user_files/skills/note-conventions/SKILL.md`.
  This is the personal override layer. On first run, the add-on separately
  seeds neutral skills for card authoring, curriculum design, curriculum
  delivery, and learning recurring card-writing preferences. Those files are
  created only when absent and are never overwritten on upgrade.
- `custom_instructions` (default empty): free-text instructions appended to the
  assistant's system prompt for **every** message (not just proposals - that's
  `conventions_prompt`). Use it for install-specific facts and policy: where
  your source files live on disk, house style for answers, standing reminders.
  It's attributed to you and marked as trusted policy that outranks anything
  card content says. Applies on your next message (the CLI re-spawns with
  `--resume`), and live-reloads if you edit it in the config editor. Keep it
  short - it rides in the system prompt on every turn, and a very long value
  bloats each request.
- `created_tag` (default `ai-created`): tag stamped on every note the assistant
  creates. Set to an empty string to add no created-by-AI tag.
- `edited_tag` (default `ai-edited`): tag stamped on any existing note the
  assistant edits (via an accepted edit proposal). Empty string = no edit tag.
- `session_tag_prefix` (default `ai-chat-dock::session-`): notes created in a
  chat also get a per-session tag (`<prefix><id>`) that powers the dock's
  "review this session's notes in the Browser" and undo-session actions. Set to
  an empty string to disable session tagging (those actions then no-op). With
  `created_tag`, `edited_tag`, and this all empty, AI notes get no automatic tags.
- `learning_nudge_threshold` (default `10`): how many edits to AI-written
  cards accumulate before the dock suggests reviewing them for patterns and
  updating the card-authoring skill (a new chat the assistant starts only
  when you click; every skill change is confirmed on a proposal card).
- `learning_nudge_days` (default `7`): also nudge when any unreviewed edit is
  older than this many days, even below the threshold.
- `pins` (managed from the dock's Pins panel, not edited here): pinned deck,
  note type, tags, and prefilled field defaults applied to every proposed note.
- `stats_refresh_minutes` (default `30`): how often the collection overview
  (deck/tag hierarchies with counts and review time) is recomputed in the
  background.
- `context_token_budget` (default `8000`): approximate size cap for the
  `get_collection_overview` tool's output (the assistant fetches the deck/tag
  overview on demand; it is no longer injected into the chat automatically).
  Larger collections get folded summaries plus drill-down tools.

Shortcut strings use Qt key-sequence syntax (`Ctrl+J`, `Ctrl+;`, `Ctrl+K`).
On macOS, `Ctrl` in these strings means the Command key.

Most preferences apply **live** when you save this config in Anki's add-on
config editor — no restart needed. That covers `vim_mode`/`vim_mappings`,
`theme`, `dock_side`, the shortcuts, `suggested_questions`, and
`open_in_claude_target`. Agent keys (`model`, `effort`, `fast_mode`,
`agent_tools`, `permission_mode`) intentionally take effect on your next message
rather than mid-chat. A few structural keys (`backend`, `claude_cli_path`, MCP
widening, `stats_refresh_minutes`) still want a restart to fully re-init.
