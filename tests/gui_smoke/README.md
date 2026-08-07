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
the smoke inside it on Anki 25.09. To verify another release during a future
compatibility expansion, pass its exact version, for example:

    make test-gui-smoke-docker ANKI_VERSION=25.07.5

Regenerate the Dockerfile after workbench upgrades with
`uv run --group dev anki-workbench dockerfile --out tests/gui_smoke/Dockerfile`.

For the stable README/demo captures, set `CWYC_PUBLIC_SCREENSHOT=1` and pass an
explicit screenshot path. After the assertions finish, the probe creates a
synthetic continuity/compactness collection, opens its current card in the
reviewer, and recreates the three public user stories. Diagnostic error states
from destructive tests never leak into the public images:

    CWYC_PUBLIC_SCREENSHOT=1 uv run --group dev \
      anki-workbench smoke --timeout 120 \
      --screenshot docs/images/chat-with-your-cards.png

The command writes the related-card story to the requested path plus
`chat-with-your-cards-explain.png` and
`chat-with-your-cards-proposal.png` beside it.

## macOS host

    make test-gui-smoke

With `anki-addon-workbench` 0.4.2 or newer, this launches a disposable Anki in
stealth mode: it avoids activation and parks the window almost entirely
off-screen. Pass `--foreground` only for an intentionally visible run.

## Fast UI iteration without Anki

`cd ui && npm run dev` — the assistant-ui frontend's Vite dev server
(http://localhost:5173) renders the real components against a scripted
replayer with a stubbed `pycmd`. Type `tool`, `propose`, `edit`, `think`,
`long`, or `error` to drive each scripted event path. See `ui/README.md`.
