# Chat With Your Cards

An Anki add-on that adds a collapsible, modern chat dock/sidebar for talking to an AI agent while studying. (Repo: `chat-with-your-cards`.)

- The current card under review is prominently in context — discussing the card is the flagship use case, but any question works.
- The agent has tools to query the collection (Anki search syntax, note/card lookup, deck/tag/stat overviews), allowed by default with configurable permission modes.
- It receives a cached, annotated description of the collection: full deck and tag hierarchies with note counts, card counts, and review-time buckets, refreshed by an in-process background job.
- It ships neutral Agent Skills for individual card authoring, curriculum design, and safe curriculum delivery, with a separate user-conventions overlay that can learn from accepted-card edits. It can propose new notes with pinnable deck/tags/note type/field defaults (set through Anki-editor-like selectors) and an optional auto-accept mode with safeguards.
- It can see the images on a card (fetched on demand as real image content), not just their filenames; audio is out of scope for now.
- Context-aware keyboard shortcuts: one chord toggles between chatting and reviewing; another starts a fresh chat.
- Backends: CLI agents (Claude Code first, Codex next) via a local MCP server, with a BYOK direct-API backend planned. See [DESIGN.md](DESIGN.md) for the trade-off analysis.

## Status

M2 complete: the agent can now author notes, safely. `propose_note` and
`propose_note_edit` write tools route through a ProposalManager (the single
write path): validation, pins-as-constraints, and proposal cards in the chat
with per-field word diffs, before/after previews rendered through the note's
real card templates, editable fields, per-field acceptance, and keyboard-first
review (Cmd+Enter / Cmd+Backspace / Cmd+arrows). Edits carry a staleness guard
(the proposal refreshes instead of applying blind if the note changed
underneath). A session ledger backs per-change revert, one-click "undo
session", and a Browser jump to this session's `ai-created` notes; auto-accept
mode applies creations only, capped per session. Conventions (prompt tier) and
pinned deck/note type/tags/field defaults shape every proposal. Verified by 17
new unit tests plus a Docker/Xvfb GUI smoke that accepts a scripted proposal
and finds the real note in the collection. Next: M3 — Codex adapter, chat
history/resume, ask-each-read, doctor panel. Architecture, milestones, and
known issues live in [DESIGN.md](DESIGN.md).

## Development

- `make check` — lint (ruff), types (mypy), unit tests (no Anki needed).
- `make test-gui-smoke-docker` — headless real-Anki smoke in Docker/Xvfb via
  [`anki-addon-workbench`](https://pypi.org/project/anki-addon-workbench/);
  the probe captures light/dark screenshots itself with `mw.grab()`.
- `cd ui && npm run dev` — the assistant-ui frontend's Vite dev server
  (http://localhost:5173), rendering the real components against a scripted
  replayer with a stubbed `pycmd`, no Anki involved. `npm run build` emits the
  committed bundle to `chat_with_your_cards/web/next/`. See `ui/README.md`.
- The add-on proper lives in `chat_with_your_cards/`; a future `.ankiaddon`
  is that directory zipped.

See `tests/gui_smoke/README.md` for details and platform caveats.
