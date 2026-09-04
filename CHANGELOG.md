# Changelog

## Unreleased

- Preserve slash-command and skill invocation at the start of outbound Claude
  messages, and show a confirmation when `/compact` completes.
- Carry manual proposal accept/reject outcomes into the agent's next real turn,
  including partial field decisions, without generating an unsolicited reply.
- Clear the old chat's change ledger and other chat-scoped review state when a
  new chat starts.

## 0.1.0 — 2026-08-06

Initial public preview.

- Add a collapsible, review-aware AI chat dock to Anki.
- Integrate the officially installed and authenticated Claude Code CLI through a token-protected loopback MCP server.
- Search and inspect notes, cards, decks, tags, scheduling state, statistics, media, and note-type structure.
- Propose reviewable note, card, deck, scheduling, media, preference, and note-type changes with validation, backups, undo, and revert paths.
- Provide explicit collection and computer-access controls, including read-only and ask-each-read modes.
- Support user-owned Agent Skills, learned card-authoring conventions, attachments, diagrams, widgets, transcripts, history, and Claude Code handoff.
- Include first-run setup guidance, a setup doctor, and a built-in demonstration backend.

Known limitations: Claude Code is the only supported backend; Windows support is experimental; no API-key authentication is supported.
