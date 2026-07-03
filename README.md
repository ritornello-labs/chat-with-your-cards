# Chat With Your Cards

An Anki add-on that adds a collapsible, modern chat dock/sidebar for talking to an AI agent while studying. (Repo: `chat-with-your-cards`.)

- The current card under review is prominently in context — discussing the card is the flagship use case, but any question works.
- The agent has tools to query the collection (Anki search syntax, note/card lookup, deck/tag/stat overviews), allowed by default with configurable permission modes.
- It receives a cached, annotated description of the collection: full deck and tag hierarchies with note counts, card counts, and review-time buckets, refreshed by an in-process background job.
- It can propose new notes that follow the user's conventions (supplied as an Agent Skill), with pinnable deck/tags/note type/field defaults and an optional auto-accept mode with safeguards.
- Context-aware keyboard shortcuts: one chord toggles between chatting and reviewing; another starts a fresh chat.
- Backends: CLI agents (Claude Code first, Codex next) via a local MCP server, with a BYOK direct-API backend planned. See [DESIGN.md](DESIGN.md) for the trade-off analysis.

## Status

M1 complete: the dock talks to a real agent. The Claude Code CLI backend runs
as a persistent headless subprocess with token-level streaming, connected to
an in-process localhost MCP server exposing nine collection read tools
(search, note/card fetch, annotated deck/tag trees, stats, clue-based
find_related). The current card under review is injected into context per
message, the collection overview comes from a background stats cache, and
everything falls back to a scripted demo backend when the CLI is absent (so
smoke tests stay deterministic). Verified by a live headless CLI check plus
green GUI smokes on macOS host (stealth) and Docker/Xvfb. Next: M2 — note
proposals. Architecture, milestones, and known issues live in
[DESIGN.md](DESIGN.md).

## Development

- `make check` — lint (ruff), types (mypy), unit tests (no Anki needed).
- `make test-gui-smoke-docker` — headless real-Anki smoke in Docker/Xvfb via
  [`anki-addon-workbench`](https://pypi.org/project/anki-addon-workbench/);
  the probe captures light/dark screenshots itself with `mw.grab()`.
- `dev/preview.html` (serve the repo, open in a browser, `?night` for dark) —
  fast chat-UI iteration with a stubbed `pycmd`, no Anki involved.
- The add-on proper lives in `chat_with_your_cards/`; a future `.ankiaddon`
  is that directory zipped.

See `tests/gui_smoke/README.md` for details and platform caveats.
