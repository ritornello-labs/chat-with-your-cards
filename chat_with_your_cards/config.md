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
- `permission_mode` (default `default`): `default` allows collection reads without
  asking; `read-only` additionally tells the assistant the session must not
  modify anything.
- `stats_refresh_minutes` (default `30`): how often the collection overview
  (deck/tag hierarchies with counts and review time) is recomputed in the
  background.
- `context_token_budget` (default `8000`): approximate token budget for the
  collection overview included in the assistant's context; larger collections
  get folded summaries plus drill-down tools.

Shortcut strings use Qt key-sequence syntax (`Ctrl+J`, `Ctrl+;`, `Ctrl+K`).
On macOS, `Ctrl` in these strings means the Command key.

Restart Anki (or reload the profile) after changing settings.
