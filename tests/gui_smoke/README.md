# GUI smoke tests

The probe add-on (`probe_addon/`) runs inside a disposable Anki profile next to
the add-on under test. It verifies the add-on imports, the dock and Tools
action register, the shortcuts exist, the webview boots (ready ping), a full
scripted chat round-trip works (JS send button -> pycmd bridge ->
ScriptedBackend -> streamed events -> DOM), and that the focus toggle returns
focus out of the dock. It captures light and dark screenshots via `mw.grab()`
(no OS screenshot permissions needed) and writes JSON to
`$ANKI_ADDON_WORKBENCH_RESULT`.

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

Serve the repo and open `dev/preview.html` (`?night` for dark mode). It loads
the real web assets, replicates Anki's `webview.css` quirks (content-box,
15px root font, global button styles), and stubs `pycmd` with a scripted
stream.
