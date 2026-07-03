# Chat With Your Cards — configuration

- `toggle_shortcut` (default `Ctrl+J`, shown as Cmd+J on macOS): context-aware chord.
  If the chat dock is hidden, shows it and focuses the message box. If focus is in
  the chat, returns focus to the reviewer/deck browser. Otherwise focuses the chat.
- `new_chat_shortcut` (default `Ctrl+Shift+J`): starts a fresh chat with fresh
  context; focus stays in the message box.
- `dock_width` (default `420`): dock width in pixels. The width you drag the dock
  to is remembered automatically when the profile closes.

Shortcut strings use Qt key-sequence syntax (`Ctrl+J`, `Ctrl+;`, `Ctrl+K`).
On macOS, `Ctrl` in these strings means the Command key.

Restart Anki (or reload the profile) after changing shortcuts.
