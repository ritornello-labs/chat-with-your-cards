# AGENTS.md

## Scope

These instructions apply to the `chat-with-your-cards` repository: "Chat With Your Cards", an Anki add-on providing a collapsible AI chat dock/sidebar.

## Source of truth

- `DESIGN.md` is the authoritative design document: architecture, backend decision (CLI-agent-first with a backend abstraction; BYOK later), tools, permission modes, note proposals, milestones, and the known-issues list.
- Keep `DESIGN.md` updated when decisions change; record resolved open questions instead of deleting them.

## Working rules

- This is a standalone Git repository inside the `anki-studying` umbrella workspace. When the project's status/scope changes, also update the workspace's `PROJECT_STATUS.md`.
- Development and visual iteration use `anki-addon-workbench` installed from PyPI via `uv` (`sfw uv ...` for installs): disposable-profile smoke tests for load/dock/menu checks, Docker/Xvfb for repeatable headless runs, and `anki-workbench launch`/`screenshot`/`click`/`type` for screenshot-driven design iteration. Inspect screenshots with vision and iterate on the CSS — do not rely on the user to eyeball every change.
- Keep the add-on AnkiWeb-shippable at all times: pure Python, no compiled dependencies, no npm build step, mutable state in `user_files/`.
- All Anki collection access happens on the main thread (`mw.taskman.run_on_main`); subprocess and MCP I/O stay on background threads.
- Never write to the collection directly from agent tools; all writes go through the proposal flow. Tag AI-created notes `ai-created`.
- Sign commits with GPG (`git commit -S`), commit and push regularly.
