# GUI smoke tests

The probe add-on (`probe_addon/`) runs inside a disposable Anki profile next to
the add-on under test. It verifies the add-on imports, the dock and Tools
action register, the shortcuts exist, the webview boots (ready ping), a full
scripted chat round-trip works (typing into the assistant-ui composer via its
`data-testid` hooks -> pycmd bridge -> ScriptedBackend -> streamed events ->
DOM: assistant markdown, tool chip, thinking-summary collapse, and a proposal
approve round-trip), and that the focus toggle returns focus out of the dock.
It captures light and dark screenshots via `mw.grab()` (no OS screenshot
permissions needed) and writes JSON to `$ANKI_ADDON_WORKBENCH_RESULT`.

## Docker/Xvfb (preferred: headless, invisible)

    make test-gui-smoke-docker

Builds the workbench-generated `Dockerfile` (Anki launcher + Xvfb) and runs
the smoke inside it. Regenerate the Dockerfile after workbench upgrades with
`uv run --group dev anki-workbench dockerfile --out tests/gui_smoke/Dockerfile`.

## macOS host (visible — avoid during normal work)

    make test-gui-smoke

Launches a real, visible disposable Anki that takes focus on macOS. Only use
when Docker is unavailable and a visible window is acceptable.

## Fast UI iteration without Anki

`cd ui && npm run dev` — the assistant-ui frontend's Vite dev server
(http://localhost:5173) renders the real components against a scripted
replayer with a stubbed `pycmd`. Type `tool`, `propose`, `edit`, `think`,
`long`, or `error` to drive each scripted event path. See `ui/README.md`.
