# AGENTS.md

## Scope

These instructions apply to the `chat-with-your-cards` repository: "Chat With Your Cards", an Anki add-on providing a collapsible AI chat dock/sidebar.

## Source of truth

- `DESIGN.md` is the authoritative design document: architecture, backend decision (CLI-agent-first with a backend abstraction; BYOK later), tools, permission modes, note proposals, milestones, and the known-issues list.
- Keep `DESIGN.md` updated when decisions change; record resolved open questions instead of deleting them.

## Working rules

- This is a standalone Git repository inside the `anki-studying` umbrella workspace. When the project's status/scope changes, also update the workspace's `PROJECT_STATUS.md`.
- Development and visual iteration use `anki-addon-workbench` installed from PyPI via `uv` (`sfw uv ...` for installs). **Do not launch visible Anki GUIs on the user's machine — they steal focus and disrupt the user's other work.** The invisible loop is: (1) `dev/preview.html` served via `python3 -m http.server` for fast webview CSS/JS iteration in a plain browser (it replicates Anki's webview.css quirks — content-box, 15px root font, global button styles — and stubs pycmd with a scripted stream); (2) `make test-gui-smoke-docker` for real-Anki verification in Docker/Xvfb. The GUI smoke probe drives the real send path and captures light+dark screenshots itself via `mw.grab()`, so no OS screenshot permissions or host GUI are needed. Inspect screenshots with vision and iterate — do not rely on the user to eyeball every change. Host-visible smoke (`make test-gui-smoke`) only with the user's explicit per-session OK; `anki-workbench launch` is additionally broken on macOS (xdotool) — `scripts/dev_launch.py` is the workaround if it is ever needed.
- Keep the add-on AnkiWeb-shippable at all times: pure Python, no compiled dependencies, no npm build step, mutable state in `user_files/`.
- All Anki collection access happens on the main thread (`mw.taskman.run_on_main`); subprocess and MCP I/O stay on background threads.
- Never write to the collection directly from agent tools; all writes go through the proposal flow. Tag AI-created notes `ai-created`.
- Sign commits with GPG (`git commit -S`), commit and push regularly.
