# Chat With Your Cards — configuration

- `toggle_shortcut` (default `Ctrl+J`, shown as Cmd+J on macOS): context-aware chord.
  If the chat dock is hidden, shows it and focuses the message box. If focus is in
  the chat, returns focus to the reviewer/deck browser. Otherwise focuses the chat.
- `new_chat_shortcut` (default `Ctrl+Shift+J`): starts a fresh chat with fresh
  context; focus stays in the message box.
- `dock_width` (default `420`): dock width in pixels. The width you drag the dock
  to is remembered automatically when the profile closes.
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
  gates the agent's own shell and file tools.
  - `"sandbox"` (default): the CLI runs with `Bash`, `Edit`, `Write`, and
    `NotebookEdit` disabled (`Read` stays read-only). The agent lives inside
    your collection; it cannot run commands or write files on your machine.
  - `"full"`: leaves those tools on and runs the CLI with
    `--permission-mode bypassPermissions` (auto-approve — every tool call runs
    with no per-command prompt). This is a power-user tier. **The catch:** the
    agent reads your card content, and card content is untrusted — a shared or
    downloaded deck can contain text crafted to steer the agent into running a
    command, and in full auto-approve mode that command runs on your computer
    immediately, with no gate. Only use `"full"` on collections you trust.
    Anki card changes still go through the review flow by default (prefer the
    built-in propose tools; a shell should use AnkiConnect, never write the
    `.anki2` file directly), but a shell can bypass that. Claude Code's built-in
    circuit breaker still blocks `rm -rf /` and `rm -rf ~`. Also selectable live
    from the dock's Model/effort picker ("Agent tools" section); switching
    respawns the CLI with `--resume` on your next message, like a model switch.
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
  widening (config-file only for now; no settings-panel UI yet).** By default
  the assistant only ever sees this add-on's own `anki` tools: the CLI is
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
  shared decks) - denials and 120s timeouts refuse the call; `auto-accept` applies the assistant's note *creations*
  immediately (up to `auto_accept_cap` per session) while edits stay behind
  proposals; `trusted-writes` applies creations, edits, bulk operations, and
  change sets directly (an Anki backup checkpoint is forced before bulk
  applies) up to `write_budget` notes per session — after that everything
  falls back to manual review. Deleting notes always asks, in every mode.
- `auto_accept_cap` (default `20`): in `auto-accept` mode, how many notes may be
  created without review per chat session before proposals pause for manual
  review again.
- `write_budget` (default `200`): in `trusted-writes` mode, how many notes the
  assistant may write directly per chat session before pausing for review.
- `conventions_prompt` (default empty): your note-authoring conventions (style,
  field usage, tagging). Injected into the assistant's instructions for every
  proposal and materialized as `user_files/skills/note-conventions/SKILL.md`.
  This is the personal override layer. On first run, the add-on separately
  seeds neutral skills for card authoring, curriculum design, curriculum
  delivery, and learning recurring card-writing preferences. Those files are
  created only when absent and are never overwritten on upgrade.
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
- `context_token_budget` (default `8000`): approximate token budget for the
  collection overview included in the assistant's context; larger collections
  get folded summaries plus drill-down tools.

Shortcut strings use Qt key-sequence syntax (`Ctrl+J`, `Ctrl+;`, `Ctrl+K`).
On macOS, `Ctrl` in these strings means the Command key.

Restart Anki (or reload the profile) after changing settings.
