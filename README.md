# anki-chat-dock

An Anki add-on that adds a collapsible, modern chat dock/sidebar for talking to an AI agent while studying.

- The current card under review is prominently in context — discussing the card is the flagship use case, but any question works.
- The agent has tools to query the collection (Anki search syntax, note/card lookup, deck/tag/stat overviews), allowed by default with configurable permission modes.
- It receives a cached, annotated description of the collection: full deck and tag hierarchies with note counts, card counts, and review-time buckets, refreshed by an in-process background job.
- It can propose new notes that follow the user's conventions (supplied as an Agent Skill), with pinnable deck/tags/note type/field defaults and an optional auto-accept mode with safeguards.
- Context-aware keyboard shortcuts: one chord toggles between chatting and reviewing; another starts a fresh chat.
- Backends: CLI agents (Claude Code first, Codex next) via a local MCP server, with a BYOK direct-API backend planned. See [DESIGN.md](DESIGN.md) for the trade-off analysis.

## Status

Design phase. No implementation yet. The full architecture, milestones, and known issues live in [DESIGN.md](DESIGN.md).

## Development (planned)

Development uses [`anki-addon-workbench`](https://pypi.org/project/anki-addon-workbench/) (installed from PyPI via `uv`) for disposable-profile smoke tests, Docker/Xvfb CI checks, and screenshot-driven visual design iteration.
