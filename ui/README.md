# cwyc-ui

Next-generation Chat With Your Cards UI, built on
[`@assistant-ui/react`](https://www.assistant-ui.com/) (headless primitives,
not the shadcn-style pre-styled components - see "Design notes" below).

This directory is a standalone dev toolchain. It has no runtime dependency on
the rest of the add-on; it only *produces* a static bundle the add-on can
load. Nothing here is wired into `chat_with_your_cards/**/*.py` yet - see
"What remains for integration".

## Install / build / preview

```sh
npm i -g sfw        # if not already installed (house rule: installs go through Socket Firewall)
cd ui
sfw npm install      # respects .npmrc's min-release-age=7
npm run build         # -> ../chat_with_your_cards/web/next/{index.html,bundle.js,bundle.css}
npm run dev            # design-iteration harness against the scripted replayer, http://localhost:5173
```

`npm run build` runs `tsc -b` (type-check only, no emit) then `vite build`.
The build target is `chat_with_your_cards/web/next/`, emitted as:

- `bundle.js` - a single self-contained IIFE (`vite.config.ts`'s `build.lib`
  with `formats: ["iife"]`), loadable via a plain `<script src="./bundle.js">`
  tag - no `type="module"`, no code-splitting, no import maps.
- `bundle.css` - all CSS in one file (`cssCodeSplit: false`).
- `index.html` - copied verbatim from `public/index.html` (Vite's
  `publicDir`); a complete, directly-openable HTML document (not a body
  fragment - see "What remains for integration").

React, ReactDOM, and `@assistant-ui/react` (with its own dependencies -
`zustand`, `radix-ui`, `assistant-stream`, etc.) are all bundled in; nothing
is fetched from a CDN at runtime. Verified by grepping the built `bundle.js`
for `import`/`export` statements (zero) and by loading it in a browser with
no network access beyond the same-origin `bundle.css`/`bundle.js` requests.

**Gotcha fixed during this build (leave `define` in `vite.config.ts` alone):**
Vite's usual automatic `process.env.NODE_ENV` replacement does not reach
library-mode/IIFE builds the way it does the default app build. Without the
explicit `define: { "process.env.NODE_ENV": JSON.stringify("production") }`
in `vite.config.ts`, React's own bundled dev-mode branches throw
`ReferenceError: process is not defined` at runtime in a plain browser (there
is no Node `process` global to fall back on, unlike in webpack). Confirmed
empirically: without the fix the bundle was 833KB and crashed on load with
that exact error; with it, 442KB and works. This is not exercised by
`npm run dev` (Vite's normal dev-server React resolution doesn't hit this
path), only by `vite build` in lib/iife mode - so `npm run build` producing a
warning-free bundle is not by itself a guarantee this is still working; if
you ever see a blank page from the built bundle with no console output, check
for this first (it throws before any of our own code runs, so React DevTools
and even `console.error` never fire; you have to eval-execute the bundle text
to see the thrown error, or check `grep -c 'process\.env' bundle.js` for
suspiciously-large counts of unguarded matches).

### Verifying the build without Anki

```sh
cd chat_with_your_cards/web/next
python3 -m http.server 8000
# open http://localhost:8000/
```

This is exactly `index.html` as it will ship - a real browser smoke test
with zero Python/Anki involved.

## Versions pinned

| package | version | published |
|---|---|---|
| `@assistant-ui/react` | `0.14.24` | 2026-06-25 |
| `react` / `react-dom` | `19.2.7` | 2026-06-01 |
| `vite` | `8.1.4` | (latest eligible at install time) |
| `@vitejs/plugin-react` | `6.0.3` | (latest eligible at install time) |
| `typescript` | `7.0.2` | (latest eligible at install time) |

All installed via `sfw npm install`, gated by `.npmrc`'s `min-release-age=7`
(the newest `@assistant-ui/react` at build time, `0.14.26`, was 6 days old
and got excluded automatically; `0.14.24` was the newest eligible release).
`package-lock.json` is committed.

`npm config get min-release-age` prints a "Unknown project config" warning on
this machine's npm (11.11.1) even though the gate demonstrably works (`npm
config list` shows the derived `before:` cutoff, and confirmed empirically:
the unpinned `react`/`react-dom` resolved to `19.2.7`, the actual latest
stable release, which happens to already be >7 days old - nothing newer was
available to test the exclusion against directly, but the mechanism is the
one documented in AGENTS.md). Not a blocker; noting it in case a future npm
upgrade changes this.

## Bundle size

- `bundle.js`: 441,942 bytes (431 KiB) uncompressed, 131,184 bytes (128 KiB) gzipped
- `bundle.css`: 8,264 bytes gzipped to 2,137 bytes

## The bridge contract

`src/bridge.ts` mirrors `chat_with_your_cards/bridge.py` exactly - the Python
side needs zero changes to load this UI instead of `web/app.js`:

- **JS -> Python**: `window.pycmd("cwyc:" + JSON.stringify({type, ...}))`.
  `postCommand()` is the one place this happens.
- **Python -> JS**: `window.chatUI.dispatch(payload)` - what `bridge.py`'s
  `Bridge.push()` evaluates. Installed by `installChatUI()`.
- **Ready handshake**: JS posts `{type:"ready"}` on a 250ms retry loop
  (`startReadyHandshake`, up to 40 attempts) until Python calls
  `window.chatUI.ackReady()` directly (not through `dispatch`) - this
  mirrors `app.js`'s `pingReadyUntilAcked()` / `__init__.py`'s
  `_mark_web_ready()` handshake exactly.
- `window.chatUI.focusComposer()` is also installed, matching
  `dock.py`'s `focus_composer()` (`self.web.eval("window.chatUI &&
  window.chatUI.focusComposer();")`).

Two "faces", both going through the same `postCommand()`/`window.pycmd` code
path (see `src/bridge.ts`'s module doc for the reasoning):

- **Real mode** (`src/main.tsx`, the production entry): `window.pycmd` is
  provided by `AnkiWebView`. Nothing else to do.
- **Dev mode** (`src/dev-main.tsx` -> `src/dev/replayer.ts`): installs a fake
  `window.pycmd` *before* the app mounts (same pattern as
  `dev/preview.html` does for `app.js`), so `postCommand()` runs unmodified
  against scripted data. Scripts are mined from
  `chat_with_your_cards/backends/fixtures.py` / `scripted.py` and
  `dev/preview.html`'s proposal fixtures; timing mirrors
  `backends/scripted.py` (25-60ms per 2-5 word delta). Type a message
  containing **tool**, **propose**, **edit**, **think**, **long**, or
  **error** to trigger that script; anything else gets the default reply. A
  short welcome turn (reasoning + text) auto-fires on load.

### Event -> UI mapping (`src/store.ts`)

`ChatStore` is a plain external store (subscribe/getSnapshot, not a React
hook - `window.chatUI.dispatch` must be callable before React has
necessarily mounted) that maps the `ChatEvent` stream onto assistant-ui's
`ThreadMessageLike` shape, fed to `useExternalStoreRuntime` via
`ChatRuntimeProvider.tsx`.

| Event (`events.ts`, mirrors `backends/base.py`'s `event_to_dict()`) | UI effect |
|---|---|
| `text_delta` | appended to a trailing `{type:"text"}` part |
| `thinking_delta` *(not real yet - see below)* | appended to a trailing `{type:"reasoning"}` part |
| `tool_call_started` | new `{type:"tool-call"}` part, `result` unset |
| `tool_call_finished` | sets `result`/`isError` on the matching part by `call_id` |
| `proposal` (`proposals.py`'s `Proposal.to_payload()`, same dict `app.js`'s `renderProposal()` reads) | `{type:"data", name:"proposal"}` part, created once and updated in place on repeat pushes for the same `id` |
| `proposal_resolved` | merges `status`/`note_id`/`warnings` into that data part |
| `proposal_error` | attaches an `errorMessage` to that data part |
| `usage` | **not** a message part - a side channel (`store.getSnapshot().usage`) the footer reads directly |
| `done` | current assistant message -> `status: {type:"complete"}`; `isRunning=false` |
| `cancelled` | current assistant message -> `status: {type:"incomplete", reason:"cancelled"}`; `isRunning=false` |
| `error` | appends a `{type:"data", name:"error"}` part, then behaves like `done`/`cancelled` (`isRunning=false`, status incomplete) |
| `reset` | clears the transcript |
| anything else | ignored, matching `app.js`'s `dispatch()` default case (forward-compatible) |

Approve/Edit/Reject on the proposal card (`components/ProposalCard.tsx`) call
`store.acceptProposal(id, fields, kind)` / `store.rejectProposal(id)`, which
send exactly `{type:"proposal_accept", id, fields, accepted_fields?}` /
`{type:"proposal_reject", id}` - the same shapes `app.js`'s
`acceptProposal()`/`rejectProposal()` send, so `ProposalManager.accept()` /
`.reject()` need no changes.

### Impedance mismatches hit against assistant-ui's model

- **`thinking_delta` does not exist upstream.** `backends/base.py`'s
  `ChatEvent` union has no reasoning event. It is stubbed *only* in
  `src/dev/replayer.ts` so the Reasoning primitive path (collapsible
  `{type:"reasoning"}` parts, `components.Reasoning` on
  `MessagePrimitive.Parts`) is proven end-to-end ahead of any real backend
  emitting it. Real `bridge.py` traffic will never send this today.
- **Proposals aren't a message-part type assistant-ui ships.** The richest
  available primitive is `MessagePart` generic `DataMessagePart<T>`
  (`{type:"data", name, data}`) plus a `components.data.by_name` renderer
  registry - this is what `ProposalCard` hooks into (`name: "proposal"`),
  the same slot used for the `error` banner (`name: "error"`). It works well,
  but it's a general escape hatch, not a purpose-built "proposal" concept -
  assistant-ui has no opinion about accept/reject/edit review flows.
- **`DataMessagePartProps<T>` collapses `data` back to `any`.** It's defined
  as `MessagePartState & DataMessagePart<T>`, and `MessagePartState`'s
  `ThreadAssistantMessagePart` union already contains an *untyped*
  `DataMessagePart<any>` member; intersecting that with our own
  `DataMessagePart<ProposalCardData>` does not narrow the way you'd expect.
  Worked around by typing `ProposalCard`'s own props directly
  (`{data: ProposalCardData; store: ChatStore}`) instead of through
  assistant-ui's generic, and passing `store` in via a closure where the
  `by_name` renderer is registered (`Thread.tsx`) rather than through props
  assistant-ui controls.
- **Tool-call part status is derived, not explicit.** There is no `status`
  field to set on a tool-call part yourself: assistant-ui computes it from
  `result` (unset -> inherits the *message's* running/complete status;
  set -> always `"complete"`) and from position for text/reasoning parts
  (only the last part in a still-running message is `"running"`; everything
  before it is locked to `"complete"` regardless of the message's own
  status). This is convenient once understood (`store.ts`'s
  `startToolCall`/`finishToolCall`/`appendText` just set/don't-set `result`
  and let positioning do the rest) but is undocumented in the public docs as
  fetched during this task - confirmed by reading
  `@assistant-ui/core`'s shipped TypeScript source
  (`src/runtime/api/message-runtime.ts`'s `toMessagePartStatus`), not the
  website.
- **Streaming text deltas require appending in place, not pushing new
  parts.** (Matches the public docs.) `store.ts`'s `appendText()` appends to
  the trailing part if it's already the same kind, else starts a new one -
  this doubles as the "break" behavior `app.js` gets from
  `breakAssistantBlock()` (a tool call or reasoning block interposed between
  two text runs naturally ends up as separate part), with no explicit break
  step needed.
- **Composer buttons via `.click()`, not synthetic `fill()`/`click()` combos
  from some browser-automation tools.** `ComposerPrimitive.Input` is a
  React-controlled textarea; some test harnesses set `.value` directly
  without going through React's native-setter/`dispatchEvent` tracking,
  which leaves the Send button `disabled` even though the DOM shows the
  typed text. Not a bug in this UI (confirmed - a real keystroke, or the
  native-setter + `dispatchEvent(new Event("input", {bubbles:true}))`
  workaround, updates it correctly every time), but worth knowing if you
  automate against this later.
- The `ExternalStoreAdapter`'s `convertMessage` is required whenever the
  store's message type isn't *exactly* `ThreadMessage` (even though
  `ThreadMessageLike` - the type it's meant to be used with - isn't either).
  `ChatRuntimeProvider.tsx` passes the identity function; it exists purely
  to satisfy the generic, does no real conversion (`store.ts` already
  produces `ThreadMessageLike`-shaped data).

## Design notes

Deliberately minimal and centralized styling (`src/styles.css`, one file,
one CSS-variable block) on top of assistant-ui's **headless primitives**
(`ThreadPrimitive`, `ComposerPrimitive`, `MessagePrimitive` from
`@assistant-ui/react` directly) - not assistant-ui's newer shadcn-style
pre-styled components (the ones the public "Installation"/"Thread UI" docs
lead with, which are copied into your repo via their CLI and expect
Tailwind + `radix-ui` + `class-variance-authority` + `tw-shimmer` + friends).
Skipped for now because: (a) it needs a network fetch from assistant-ui's
component registry at dev time, (b) it pulls in a much larger dependency
tree for a scaffold whose event-mapping correctness matters far more than
visual polish right now, and (c) this scaffold already has its own custom
`ProposalCard`/`ToolCallCard`/`ReasoningBlock` components that don't map
onto their prebuilt `Thread` component anyway. `@assistant-ui/react`'s
*primitives* (used here) have no Tailwind/radix-styling dependency at the
package level - confirmed via `npm view @assistant-ui/react@0.14.24
peerDependencies dependencies`.

CSS variables (`--cwyc-*`) are self-contained, **not** read from Anki's
injected `--canvas`/`--fg`/`--border` custom properties the way
`chat_with_your_cards/web/styles.css` does - this bundle doesn't yet know how
the eventual Python loader will wire it up. `prefers-color-scheme` is the
default (relevant standalone / in the dev preview / in any plain browser);
`body.night-mode` is Anki's own signal and wins whenever present (it has
higher CSS specificity than the bare `:root` rules), matching the existing
`web/styles.css` convention. Known gap: an Anki *light* theme running inside
an OS set to dark falls back to the OS signal, since Anki has no equivalent
"day-mode" class to detect that case explicitly - not solvable from CSS
alone without a real signal from Python, and out of scope for this pass.

Not ported from `app.js` (intentionally out of scope for this scaffold -
restyle-later or integration-later, not forgotten):

- Markdown rendering (`marked.js` in `app.js`; plain text here)
- The word-level diff view on edit-proposal fields
- Friendly tool-name labels + hiding internal tools (`search_notes` ->
  "Searched your cards", `ToolSearch` hidden) - every tool call renders
  generically here
- Card preview iframes (rendered templates) on proposal cards
- Everything outside the core thread: pins panel, agent/model picker,
  permission-mode chip, doctor panel, chat history panel, ledger strip,
  bulk accept/reject bar, learning nudge, suggested-questions ghost text,
  keyboard shortcuts (Cmd+Enter to accept, Shift+Tab to cycle mode, etc.)
- Bulk/delete/change_set/deck_op/skill_update proposal kinds render (kind
  badge, rationale, warnings, count, Accept/Reject) but without their
  kind-specific body (no diff samples, no change-set item list, no skill
  diff view) - only `create`/`edit` get field-level editing, matching the
  task's explicit Approve/Edit/Reject scope

## What remains for integration (NOT done here, by design)

This UI does not wire itself into the add-on. Specifically, someone (not
this pass) needs to:

1. **Add a Python-side flag/setting to load `web/next/` instead of
   `web/`.** `chat_with_your_cards/dock.py`'s `_load_ui()` currently does:

   ```python
   body = (_WEB_DIR / "index.html").read_text(encoding="utf-8")
   self.web.stdHtml(body=body, css=[f"{base}/styles.css"], js=[f"{base}/vendor/marked.min.js", f"{base}/app.js"])
   ```

   `_WEB_DIR` would need to become configurable, pointed at
   `web/next/`. **Important:** `chat_with_your_cards/web/next/index.html` is
   a *complete* HTML document (`<html><head><link>...<body>...<script>`),
   not the body-only fragment `stdHtml(body=...)` expects - unlike
   `web/index.html`. Either extract just the inner
   `<div id="cwyc-root"></div>` and keep using `stdHtml(body=..., css=[...],
   js=[...])`, or switch to loading `web/next/index.html` directly (e.g. via
   `web.load_url`/`QUrl.fromLocalFile`), bypassing `stdHtml` for this path.
   Both are one-line-ish changes; which one is right depends on whether
   `stdHtml`'s other injected context (Anki's own CSS variables, `pycmd`
   bootstrapping, etc.) is still wanted for this bundle. Not decided here.
2. **Verify `window.pycmd` timing against a real `AnkiWebView`.** This UI's
   ready handshake (`startReadyHandshake`) assumes the same 250ms/40-attempt
   retry `app.js` already uses successfully in production; not re-verified
   against a live Anki webview in this pass (only against a plain browser
   and the scripted replayer).
3. **Decide the CSS variable source** (see "Design notes" above): keep this
   bundle's self-contained `--cwyc-*` variables, or rewire them to read
   Anki's injected `--canvas`/`--fg`/`--border` the way `web/styles.css`
   does, for tighter visual consistency with the rest of Anki's chrome.
4. **Restyle pass.** Per the task, this pass is deliberately
   default-assistant-ui-primitive-plain. A follow-up pass should bring
   visual parity with `web/styles.css` (or move past it) and close the
   "Not ported from app.js" gaps above, prioritized by what's actually
   blocking a real cutover.
