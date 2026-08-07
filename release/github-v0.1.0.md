# Chat With Your Cards v0.1.0

The first public preview adds a review-aware Claude Code assistant directly to Anki. It can discuss the current card, search the collection, and stage inspectable changes behind explicit proposal and safety controls.

Highlights:

- Claude Code harness integration with streaming chat and session history
- read and proposal tools for notes, cards, decks, tags, scheduling, media, preferences, and note types
- user-owned card-authoring and curriculum skills
- sandboxed default computer access, loopback tool authentication, backups, undo, and proposal review
- first-run setup guidance, diagnostics, demonstration mode, and built-in privacy documentation

Requirements: Anki 25.09 and Claude Code 2.1.220 or newer. macOS and Linux are the supported platforms for this preview; Windows support is experimental. CWYC uses Claude Code's official login and does not accept API keys.

Install the attached `chat-with-your-cards.ankiaddon` from Anki's add-on installer. Files installed from GitHub releases do not receive Anki's automatic add-on updates; install from AnkiWeb once that listing is available if you want automatic updates.

See [PRIVACY.md](https://github.com/ritornello-labs/chat-with-your-cards/blob/v0.1.0/PRIVACY.md) and [SECURITY.md](https://github.com/ritornello-labs/chat-with-your-cards/blob/v0.1.0/SECURITY.md) before enabling broader computer or MCP access.
