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
- `suggested_questions` (default `true`): show a context-aware suggested
  question as gray ghost text in the empty composer; Tab accepts it, typing
  dismisses it.
- `web_access` (default `true`): allow the assistant to use web search and
  page fetching (useful for sourcing card content). Set `false` to keep the
  agent strictly inside your collection.
- `compact_tool_descriptions` (default `true`): advertise one-line tool
  summaries to the agent (full docs available on demand via its tool_help
  tool) to save context. Schemas are always complete.
- `anthropic_api_key` / `openai_api_key` (default empty): paste an API key to
  bill usage to it instead of the agent's own login. Stored in Anki's
  plain-text add-on config - the less-secure option.
- `anthropic_api_key_op` / `openai_api_key_op` (default empty): a 1Password
  reference (`op://Vault/Item/field`) resolved via the `op` CLI when the
  agent starts; the secret never touches disk. Takes precedence over the
  pasted key.
- `permission_mode` (default `default`): `default` allows collection reads without
  asking and gates all writes behind proposal cards; `read-only` removes the
  write tools entirely; `auto-accept` applies the assistant's note *creations*
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
- `created_tag` (default `ai-created`): tag stamped on every note the assistant
  creates. Set to an empty string to add no created-by-AI tag.
- `edited_tag` (default `ai-edited`): tag stamped on any existing note the
  assistant edits (via an accepted edit proposal). Empty string = no edit tag.
- `session_tag_prefix` (default `ai-chat-dock::session-`): notes created in a
  chat also get a per-session tag (`<prefix><id>`) that powers the dock's
  "review this session's notes in the Browser" and undo-session actions. Set to
  an empty string to disable session tagging (those actions then no-op). With
  `created_tag`, `edited_tag`, and this all empty, AI notes get no automatic tags.
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
