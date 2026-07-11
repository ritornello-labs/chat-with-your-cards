# AGENTS.md

## Scope

These instructions apply to the `chat-with-your-cards` repository: "Chat With Your Cards", an Anki add-on providing a collapsible AI chat dock/sidebar.

## Source of truth

- `DESIGN.md` is the authoritative design document: architecture, backend decision (CLI-agent-first with a backend abstraction; BYOK later), tools, permission modes, note proposals, milestones, and the known-issues list.
- Keep `DESIGN.md` updated when decisions change; record resolved open questions instead of deleting them.

## Working rules

- This is a standalone Git repository inside the `anki-studying` umbrella workspace. When the project's status/scope changes, also update the workspace's `PROJECT_STATUS.md`.
- Development and visual iteration use `anki-addon-workbench` installed from PyPI via `uv` (`sfw uv ...` for installs). **Do not launch visible Anki GUIs on the user's machine — they steal focus and disrupt the user's other work.** The invisible loop is: (1) `cd ui && npm run dev` — the assistant-ui Vite dev server (http://localhost:5173) rendering the real frontend components against the scripted replayer (`ui/src/dev/`), for fast design/CSS/JS iteration in a plain browser; type `tool`, `propose`, `edit`, `think`, `long`, or `error` to trigger each scripted event path; (2) `make test-gui-smoke-docker` for real-Anki verification in Docker/Xvfb. The GUI smoke probe drives the real send path and captures light+dark screenshots itself via `mw.grab()`, so no OS screenshot permissions or host GUI are needed. Inspect screenshots with vision and iterate — do not rely on the user to eyeball every change. Since anki-addon-workbench 0.4.2, host smoke and launch on macOS run in **stealth mode by default** (window shown without activation, parked off-screen except a 2px sliver), so host runs no longer steal focus; until 0.4.2 is on PyPI, run the local workbench with `uv run --project ../anki-addon-workbench anki-workbench ...`. Visible runs require `--foreground` and the user's explicit per-session OK.
- Keep the add-on AnkiWeb-shippable at all times: pure Python, no compiled dependencies, no npm build step, mutable state in `user_files/`.
- All Anki collection access happens on the main thread (`mw.taskman.run_on_main`); subprocess and MCP I/O stay on background threads.
- Never write to the collection directly from agent tools; all writes go through the proposal flow. Tag AI-created notes `ai-created`.
- Sign commits with GPG (`git commit -S`), commit and push regularly.
