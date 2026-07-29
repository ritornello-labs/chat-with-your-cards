# cwyc-ui

Next-generation Chat With Your Cards UI, built on
[`@assistant-ui/react`](https://www.assistant-ui.com/) (headless primitives,
not the shadcn-style pre-styled components - see "Design notes" below).

This directory is a standalone dev toolchain. It has no runtime dependency on
the rest of the add-on; it only *produces* the static bundle the add-on loads
from `chat_with_your_cards/web/next/`. As of 2026-07-11 this is the add-on's
**only** UI (the old vanilla-JS `web/` UI was deleted) - see "Integration
status" below.

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
| `marked` | `18.0.5` | 2026-06-04 |
| `dompurify` | `3.4.11` | 2026-06-17 |
| `vite` | `8.1.4` | (latest eligible at install time) |
| `@vitejs/plugin-react` | `6.0.3` | (latest eligible at install time) |
| `typescript` | `7.0.2` | (latest eligible at install time) |

Both `marked` and `dompurify` ship their own TypeScript types (no `@types/*`
needed). All installed via `sfw npm install`; `package-lock.json` is
committed. The 7-day age gate is enforced by hand-pinning on this machine's
npm (11.11.1 warns "Unknown project config min-release-age" — it does not
honor `.npmrc`'s `min-release-age=7` natively), so the newest *eligible*
release was picked explicitly: `marked` `18.0.6` (2026-07-09, 2 days old)
and `dompurify` `3.4.12` (2026-07-11, same-day) were both too fresh and were
pinned back to `18.0.5` / `3.4.11`; `@assistant-ui/react` `0.14.26` (6 days)
was likewise excluded in favor of `0.14.24`.

`npm config get min-release-age` prints a "Unknown project config" warning on
this machine's npm (11.11.1) even though the gate demonstrably works (`npm
config list` shows the derived `before:` cutoff, and confirmed empirically:
the unpinned `react`/`react-dom` resolved to `19.2.7`, the actual latest
stable release, which happens to already be >7 days old - nothing newer was
available to test the exclusion against directly, but the mechanism is the
one documented in AGENTS.md). Not a blocker; noting it in case a future npm
upgrade changes this.

## Bundle size

- `bundle.js`: 514,540 bytes (502 KiB) uncompressed, ~156.7 KiB gzipped
- `bundle.css`: 121,148 bytes (118 KiB), ~83.9 KiB gzipped

(As of the markdown pass, 2026-07-11: **+68,600 bytes JS (+67.0 KiB)** over
the restyle's 445,940 — `marked` + `dompurify` bundled in at build time
(zero runtime network) — and **+1,620 bytes CSS (+1.6 KiB)** for the
`.cwyc-markdown` styles. The prior "Reading lamp" restyle pass added +2,810
bytes JS over its own 443,130 baseline plus ~111 KB CSS, almost entirely the
four IBM Plex woff2 latin subsets in `src/assets/fonts/` that Vite's lib mode
inlines into `bundle.css` as base64 `data:` URIs. The inlining is what keeps
the bundle a strict three-file artifact with zero runtime network fetches —
verified against the browser's network log: only same-origin
`index.html`/`bundle.css`/`bundle.js` requests.)

## The bridge contract

`src/bridge.ts` mirrors `chat_with_your_cards/bridge.py` exactly - so the
Python side needed zero changes to load this UI in place of the old
hand-rolled vanilla-JS UI:

- **JS -> Python**: `window.pycmd("cwyc:" + JSON.stringify({type, ...}))`.
  `postCommand()` is the one place this happens.
- **Python -> JS**: `window.chatUI.dispatch(payload)` - what `bridge.py`'s
  `Bridge.push()` evaluates. Installed by `installChatUI()`.
- **Ready handshake**: JS posts `{type:"ready"}` on a 250ms retry loop
  (`startReadyHandshake`, up to 40 attempts) until Python calls
  `window.chatUI.ackReady()` directly (not through `dispatch`) - this
  mirrors `__init__.py`'s `_mark_web_ready()` handshake exactly (the same
  contract the previous UI used).
- `window.chatUI.focusComposer()` is also installed, matching
  `dock.py`'s `focus_composer()` (`self.web.eval("window.chatUI &&
  window.chatUI.focusComposer();")`).

Two "faces", both going through the same `postCommand()`/`window.pycmd` code
path (see `src/bridge.ts`'s module doc for the reasoning):

- **Real mode** (`src/main.tsx`, the production entry): `window.pycmd` is
  provided by `AnkiWebView`. Nothing else to do.
- **Dev mode** (`src/dev-main.tsx` -> `src/dev/replayer.ts`): installs a fake
  `window.pycmd` *before* the app mounts, so `postCommand()` runs unmodified
  against scripted data. Scripts are mined from
  `chat_with_your_cards/backends/fixtures.py` / `scripted.py` plus a couple of
  proposal fixtures reimplemented in the replayer; timing mirrors
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
| `text_delta` | appended to a trailing `{type:"text"}` part, rendered as sanitized markdown (`marked` → DOMPurify, `src/markdown.ts` / `TextPart.tsx`) on every delta |
| `thinking_delta` (real since 2026-07-11 - see below) | appended to a trailing `{type:"reasoning"}` part, `text` pinned to `THINKING_SENTINEL` while no real text has streamed |
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

New-note proposals are adapted by `interactionAdapter.ts` to the interaction
*presentation standard* — an `InteractionPresentation` from the vendored
`@elvis-labs/interaction-schema` package — and rendered by the vendored
`@elvis-labs/interaction-ui-react` package — vendored as a tarball; bumped to
**0.6.0** on 2026-07-27 to add `renderFieldEditor`, a host-supplied
click-to-edit control (same contract as `renderBlock`: return a node to take
over, `undefined` to keep the built-in textarea). That is how vim keys reach
the create card's fields without the shared package growing an editor
dependency — it owns the draft, the host owns the control.
The package is (the 2026-07-16 split of the old
combined `@elvis-labs/interaction-ui` 0.1.0; visuals unchanged). The renderer
is presentation-only: it treats `revision`/`digest`/ids as opaque tokens and
echoes them byte-for-byte; the adapter owns status→badge labels and offers
actions only while a proposal is `pending`; `ProposalCard.tsx` converts the
echoed revision back to the bridge's number exactly at the boundary. CWYC
imports no broker/lifecycle code — the Driver broker client is a third,
separate module this repository never depends on.

Editing is a two-step exact-revision flow: Save revision sends
`{type:"proposal_revise", id, expected_revision, fields}`; ProposalManager
validates and previews the fields, increments the revision, and pushes it
back; Add note then sends `{type:"proposal_accept", id, revision}`. A stale
revision is rejected before the Anki write chokepoint. Other proposal kinds
retain the legacy React renderer and bridge commands until their
presentation adapters exist.

The canonical package sources live in a separate first-party repository
that is not yet public. CWYC commits the exact `npm pack` tarballs under
`ui/vendor/`, so this repository does not depend on a sibling checkout or a
private registry — the packages are deliberately not on public npm yet
(DESIGN.md "Interaction-ui posture").

### Impedance mismatches hit against assistant-ui's model

- **`thinking_delta` text is redacted upstream at every reasoning effort
  level observed from the real CLI (landed 2026-07-11, `backends/base.py`'s
  `ThinkingDelta` now carries `estimated_tokens` too).** The account/CLI
  streams `thinking_delta` stream events with an empty `thinking` string
  throughout - only an opaque, encrypted `signature_delta` carries the real
  content - so `text` on the reasoning part stays empty in practice, and
  `estimated_tokens` is the only signal a thinking phase is live.
  assistant-ui's `fromThreadMessageLike` (`@assistant-ui/core`, not the
  public docs - confirmed by reading the shipped source) drops any
  `{type:"reasoning"}` part whose `text` is empty/whitespace-only when
  converting `ThreadMessageLike` -> `ThreadMessage`, which would otherwise
  make the part - and the indicator it carries - vanish outright while
  `text` is empty. Worked around with `store.ts`'s `THINKING_SENTINEL` (a
  zero-width space, not stripped by `.trim()`) standing in for `text`
  whenever no real thinking text has accumulated; `ReasoningBlock.tsx`
  checks for the sentinel to drive a rotating "Thinking… ~N tokens"
  indicator instead of rendering it as visible text. `estimatedTokens`
  itself is not part of assistant-ui's `ReasoningMessagePart` type, but
  rides along as an extra field on the part object - `MessageParts.js`
  spreads the whole part into the rendered component's props
  (`jsx(Reasoning, {...part})`), so it survives untouched.
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

**Restyled 2026-07-11 ("Reading lamp"):** warm paper neutrals (`#faf9f6`
light / `#1e1e1c` warm charcoal dark), one saffron-amber accent
(`#d99a2b` light / `#e6b45c` dark, dark-text-on-amber fills - never white),
desaturated green/red semantics, IBM Plex Sans + IBM Plex Mono bundled as
latin-subset woff2 (OFL; `src/assets/fonts/`, inlined into `bundle.css` at
build time - no CDN). Signature moments: the proposal card framed as a
physical flashcard with a 3D front/back (or before/after) flip on the
rendered-card preview (sandboxed srcdoc iframes, same face semantics as
`app.js`'s `buildPreviewTabs`), and the "thinking ember" - a 2.5s breathing
amber dot on the live reasoning indicator that goes cold and static on the
collapsed "Thought for ~N tokens" line. Motion is limited to the streaming
caret, the 150ms tool-chip expand, the card flip, and the ember, all behind
a `prefers-reduced-motion` kill switch. Every text/background pair was
checked >= 4.5:1 (WCAG AA) in both modes.

**Test hooks:** stable `data-testid` attributes, independent of styling
classes, for the GUI probe: `composer-input`, `send`, `stop`,
`assistant-message`, `user-message`, `tool-chip`, `thinking-indicator`
(live), `thinking-summary` (collapsed/done), `proposal-card`,
`proposal-approve`, `proposal-edit`, `proposal-reject`, `error-banner`.

All styling remains centralized (`src/styles.css`, one file, one
CSS-variable block per theme) on top of assistant-ui's **headless primitives**
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
injected `--canvas`/`--fg`/`--border` custom properties (the way the old
vanilla-JS UI's stylesheet did) - this bundle keeps its own palette.
`prefers-color-scheme` is the default (relevant standalone / in the dev
preview / in any plain browser); `body.night-mode` is Anki's own signal and
wins whenever present (it has higher CSS specificity than the bare `:root`
rules), the same night-mode convention the previous UI used. Known gap: an
Anki *light* theme running inside
an OS set to dark falls back to the OS signal, since Anki has no equivalent
"day-mode" class to detect that case explicitly - not solvable from CSS
alone without a real signal from Python, and out of scope for this pass.

Not ported from `app.js`. **Re-audited 2026-07-23** — the previous version of
this list was both stale (several items had since shipped) and, more
importantly, *incomplete*: it omitted every item marked ⚠ below, including a
permission mode that hangs. Treat this as the record, and keep it honest — a
missing entry here is how the word-level diff stayed unimplemented for a year
while this file claimed it was a known gap.

**Since shipped** (were on this list, no longer gaps): word-level field diff
(2026-07-23, now `wordDiff` from `@elvis-labs/interaction-ui-react`), the
**ask-each-read approval chip** (2026-07-23 — see below), pins panel,
agent/model picker, permission-mode chip + Shift+Tab, doctor panel, chat
history panel. Cmd+J / Cmd+Shift+J were not lost either — they moved to Qt
shortcuts (`shortcuts.py`), which is the correct home.

**Fixed 2026-07-23 — `tool_approval`.** Was: never handled in `store.ts`, so
"Ask each read" (the first entry in the mode picker) hung for 120s and then
auto-denied, because Python blocks on `approvals.py`'s `event.wait(120)` for a
response the UI could not send. Now `ToolApprovalChip` renders the request with
its raw tool arguments, Allow autofocused for a keyboard-only loop, and posts
`tool_approval_response`. The chip marks itself resolved optimistically on
click; Python's echo is idempotent. Covered by a GUI-smoke check that drives
the **real** broker on a background thread and asserts the click releases it.
A follow-up fixed the blocking model behind the chip: the call now waits only
~10s (`APPROVAL_GRACE_S`) and then returns *pending* rather than blocking past
the MCP client's own tool timeout — the chip stays answerable, and an answer
given late is consumed by the agent's next attempt.

**⚠ Functional breakage (was never recorded here):**

- ~~The **session ledger has no entry point at all**~~ — **fixed 2026-07-27
  (#18)**: `LedgerStrip.tsx` sits above the composer with a one-line summary
  that expands per change, plus Undo-all (confirmed) and the Browser jump.
- ~~**Per-proposal Revert / Re-add / Restore**~~ — **fixed 2026-07-27 (#18)**:
  `ProposalActions.tsx` renders under a resolved card — Undo when applied,
  Re-apply when undone, Put-back-for-review when rejected/superseded, and an
  explanation instead of a button when only an Anki backup can undo it.
  Revert also refuses to discard a change made after ours and offers an
  explicit override (see proposals.py's `StaleRevert`).
- ~~**`proposal_supersede` is never sent**~~ — **fixed 2026-07-27 (#19)**:
  "Suggest change" seeds the composer with a reference to the proposal and
  arms the supersede, which fires when the message is actually SENT (until
  then the proposal is still the live offer).
- ~~**Tags are invisible on proposals**~~ — **fixed 2026-07-27 (task #20a)**.
  `ProposalTags.tsx` draws the note's current tags plus the `+ added` /
  `− removed` delta on edit proposals, filtered against what the note actually
  carries so a no-op add/remove is not drawn as a change.
- ~~The **`open` flag is ignored**~~ — **fixed 2026-07-27 (#19)**: a change
  set still collecting edits says "Collecting edits… N note(s) so far" and
  offers no actions, on both card variants.
- ~~⚠ **Accessibility**: the transcript lost its `aria-live="polite"`~~ —
  **fixed 2026-07-27 (#22)**, but NOT by restoring the attribute. A reply
  streams token by token, so a live region over the transcript re-announces
  the growing text on every delta. `Announcer.tsx` is a visually hidden
  `role="status"` region fed by settled state (turn started / reply finished
  with anything now awaiting a decision / failed / stopped); the viewport is
  `role="log"` with an explicit `aria-live="off"` so the role does not smuggle
  the per-token announcements back in.
- ⚠ **Escape returns focus to the reviewer only in vim mode.** The old handler
  was deliberately *capture-phase* to beat AnkiWebView's own bubble-phase
  Escape → `pycmd("close")`; preserve that detail in any fix.

**Known gaps, previously recorded and still open:**

- ~~Live preview-while-typing on **edit** proposals~~ — **fixed 2026-07-27
  (#20b)**: `ProposalCard` debounces 400ms and posts `proposal_preview`;
  the store applies `preview_update` to `previews` only (never status,
  fields, or revision) and drops one that arrives after the card resolved.
- ~~Deck is read-only on a create proposal~~ — **fixed 2026-07-27 (#20c)**:
  deck (a free-text ComboBox — a proposal often targets a deck that does not
  exist yet) and tags are both editable, and ride the accept message that
  `_accept_create` already honoured.
- Friendly tool-name labels + hiding internal tools — every call still renders
  its raw tool name.
- Bulk accept/reject bar, learning nudge, suggested-questions ghost text,
  context chip.
- Proposal keyboard review: Cmd+Enter accept, Cmd+Backspace reject,
  Cmd+Up/Down to cycle.
- ~~Bulk/delete/change_set/deck_op/skill_update kinds have no kind-specific
  body~~ — **fixed 2026-07-27 (#20d)**: `ProposalBody.tsx` renders the
  operation label, a counted noun that says what is counted, one collapsible
  list of affected notes with word-level diffs inline on the sampled ones, and
  the unified diff for a skill update. Field-level *editing* is still
  `create`/`edit` only.

## Integration status

**Integrated 2026-07-11 — this is now the only UI.** The hand-rolled
vanilla-JS `web/` UI and its browser dev harness were deleted;
`chat_with_your_cards/dock.py`'s `_load_ui()` unconditionally loads this
bundle from `web/next/` through `stdHtml()` (a `<div id="cwyc-root"></div>`
body fragment plus `bundle.js`/`bundle.css` web exports — Anki never loads
the standalone `web/next/index.html`), and the `ui` config flag was removed.
The GUI smoke probe (`tests/gui_smoke/probe_addon/`) drives this UI's
`data-testid`s against a real `AnkiWebView`, confirming the `window.pycmd`
timing / ready handshake and the full send → stream → proposal round-trip.

Done since the scaffold: the "Reading lamp" restyle (see "Design notes"),
and **markdown rendering** (`marked` → DOMPurify, `src/markdown.ts` /
`TextPart.tsx` — sanitized because model output is untrusted, streaming-safe
because it re-renders on every delta).

Still open:

- The CSS variable source stays this bundle's self-contained `--cwyc-*`
  variables (see "Design notes"); rewiring them to Anki's injected
  `--canvas`/`--fg`/`--border` for tighter chrome consistency is a possible
  future pass.
- Everything under "Not ported from `app.js`" above — re-audited 2026-07-23
  and filed in the working backlog. Note that several of those are *not*
  cosmetic parity: the unreachable ledger/revert path affects correctness and
  safety. (Invisible proposal tags, the stale edit preview, the read-only deck,
  and the missing kind bodies were closed on 2026-07-27; the strikethroughs
  above mark them.)
