# Backend and conversation plan — 2026-11-02

Status: product recommendation and implementation plan, not implemented.
The current implementation task is the MCP settings manager. Dependency graphs,
capstone gating and their progress UI are explicitly deferred. The cross-device
long-recall tutor remains an earlier, unbuilt idea; asking what it means does
not authorize building it.

## Assessment of the original design

Keep the harness-based architecture: a local authenticated agent supplies the
agent loop; CWYC supplies Anki context, tools, proposals and the study interface.
Keep collection access enforced in Python independently of harness permissions.
Keep the add-on pure Python with a committed frontend bundle. Each harness is a
user-installed executable; no embedded SDK runtime or credential manager.

Revise three assumptions:

- Prefer Codex's persistent app-server protocol over `codex exec` for this UI.
  It provides incremental messages, approvals, interruption, resume and branching.
- Pi now documents native MCP. Begin with RPC and native MCP, instead of
  precommitting to a bridge extension. Older installations need a compatibility
  result, not an automatic upgrade.
- Define fork/rewind behavior before writing adapters. The visible transcript
  and model history must agree. Anki mutations are shared state with separate
  undo semantics; hiding bubbles cannot rewind either the model or collection.

## Product behavior

### Choose an assistant once; keep studying

Settings offers Claude Code, Codex and Pi with availability, setup guidance and
a deliberate demo choice. New chats use the selected harness; existing chats
remember their harness, model and native session. Never silently substitute a
provider when the selected one fails.

Switching models within a supported harness can preserve the session. Changing
harness starts a new chat. A later "Continue with another assistant" action
could carry a disclosed summary; it must not claim to transfer hidden reasoning
or native tool history. Populate model/effort choices from adapter capabilities.
Keep backend diagnostics in Settings and the main study composer simple.

Declare capabilities for resume, fork at head, fork at a historical boundary,
interruption, images, skills, approvals and usage. Show actions only when they
work in the installed version. No lowest-common-denominator UI or fake parity.

### Branch to explore another explanation

"Branch from here" on a completed turn creates a separate chat with an origin
link; history keeps both alternatives. Copy conversation content through the
chosen boundary, then refresh current card/collection context before the next
message. Explain once that both branches use the same current collection.

Historical proposals are records, not fresh Accept buttons. Pending proposals
in the copied prefix become inactive. Accepted writes are not replayed. New
proposals, grading cursors, write budgets and permissions belong to the new
session. Reject late events from the old session after any switch.

### Edit and retry without losing an explanation

"Edit and retry" on an earlier user message opens its text in the composer.
Sending creates an alternative from the preceding completed boundary; the
original stays available. Canceling does nothing. Initially support stable,
completed boundaries; stop a running turn and await its terminal event first.

If later turns applied collection changes, show: "Your Anki changes remain.
Review changes to undo them." Do not automatically reverse scheduling, delete
created notes or restore a historical collection. Existing ledger undo retains
its staleness/backup rules; do not offer cross-restart undo without its required
state. If a harness lacks a supported historical-boundary API, hide that action
for that version. Never parse/rewrite private session files to approximate it.

### Fresh skills on New chat

The controller already rebuilds its backend on New chat, but skills and the
conventions mirror are materialized during add-on setup. Add one shared
preparation step before prewarming a new conversation: read current user-owned
skills/conventions, seed only missing factory skills, regenerate derived mirrors,
and let the harness rediscover them. Reuse it for background sessions. Never
overwrite customized skills with bundled templates.

Record a private skill fingerprint in conversation metadata for diagnostics.
Mid-chat changes take effect on New chat, with a compact notice when useful.
A later explicit Refresh skills action can restart/resume at an idle boundary
if the adapter verifies it. Rereading files and upgrading factory templates are
different operations; customized template upgrades require a reviewable diff.

## Implementation order and completion criteria

1. **Shared session foundations and skill refresh.** Add stable turn/item IDs,
   harness identity, branch lineage and capabilities to transcript metadata with
   backward-compatible loading. Test New chat, history resume, background
   analysis and teardown. Prove edited skills appear in the next session and
   custom files survive preparation.
2. **Codex vertical slice.** Manage an installed `codex app-server` child via its
   supported stdio protocol, initialize, connect only intended MCP servers, and
   start/resume turns. Normalize streamed messages, tools, usage, approvals,
   errors and interrupts into CWYC events. Keep auth in the installed harness.
   Verify a synthetic current-card explanation, search, reviewed proposal,
   cancellation, resume and shutdown before exposing the selector. Establish a
   tested minimum version from the spike.
3. **Branch and edit/retry.** Extend Claude for native head forks and Codex for
   supported historical boundaries. Enable Claude historical branching only
   after verifying a documented boundary API for the chosen transport. Test
   exact prefix agreement between native/model and CWYC histories, branch
   isolation, inactive old proposals, no repeated writes, restart recovery and
   late events. Start with per-message actions and lineage links; defer a full
   tree visualization until actual use warrants it.
4. **Pi vertical slice.** Use `pi --mode rpc`, request IDs and native session
   operations. Test current native MCP first. Translate common configuration
   while preserving provider-specific advanced options. Pi documents stdio and
   streamable HTTP; legacy SSE needs an explicit unsupported result. Verify
   inherited-server isolation, tool disabling, Anki proposals and permissions
   before calling it supported. Build a bridge only if a tested version needs it.
5. **Product qualification.** Run the same explanation, prerequisite-search
   and note-repair scenarios across adapters, then deliberate dogfooding. Assess
   lost context, unnecessary questions, proposal correctness, time to useful
   output, cancellation and recovery from missing auth/process failure. Keep
   private transcripts outside Git. Ship each adapter when it meets the shared
   contract, not merely when its command launches.

No schedule or model-spend budget is inferred. Implementation beyond the MCP
manager awaits a follow-up request.

## Evidence and bounded unknowns

- [Codex app-server](https://learn.chatgpt.com/docs/app-server) documents
  `thread/fork`, turn streaming, interruption and `skills/list` force reload.
  The current history API is `thread/revert`; `thread/rollback` is removed.
  Revert affects conversation history, not file changes. Our edit/retry design
  still prefers preserving the original by branching. Exact installed-version
  schemas and permission behavior need a synthetic integration probe.
- [Claude CLI reference](https://code.claude.com/docs/en/cli-reference) documents
  `--resume --fork-session`. This establishes head branching, not arbitrary
  historical boundaries in the current subprocess adapter. Verify a supported
  interface before enabling historical editing on Claude.
- [Pi RPC](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc.md)
  and [session commands](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc-commands.md)
  document a persistent subprocess and native fork/clone operations.
  [Pi MCP](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/mcp.md)
  documents native transports and project/user configuration, superseding the
  July "no built-in MCP" assumption for current upstream.
- Read-only local checks found Claude Code 2.1.284, Codex 0.159.0 and Pi 0.80.3.
  No model turns, account changes or upgrades were run. Current upstream docs
  are not proof of capability in these exact installed binaries.
