# Backend and conversation plan — 2026-11-02

Status: revised product requirements; isolated runtime comparison completed;
production backend changes not implemented.
The MCP settings manager and agent-facing Anki profile-setting tools are
implemented in the current source PR. The MCP screen manages external
connections; get/set_anki_settings manage Anki, and get/set_addon_settings
manage ordinary CWYC preferences. Dependency graphs and capstone gating remain deferred.
The long-recall tutor is important retained product direction, explicitly for
later; no tutor implementation is authorized in the current work.

## Requirements clarified after the initial plan

Support both subscription access and API-key access as first-class options.
Pi is neither a requirement nor a committed milestone. A harness, SDK or direct
API loop is an implementation choice to evaluate against the product needs.
This supersedes the August harness-only/API-exclusion decision and the earlier
November recommendation to build Pi as a required adapter.

The [completed comparison](RUNTIME_COMPARISON_2026-11-02.md) recommends keeping
Claude Code and adding Codex app-server, with subscription and API access through
each official runtime. Pi passed its protocol tests; defer it until additional
API providers justify the extra runtime. Do not build a custom API loop now.

The core product should make studying better: useful current-card explanations,
accurate prerequisite search, reliable reviewable note changes, and consistent
conversation controls. Provider/runtime breadth is useful when it improves that
experience, not as an end in itself.

## Assessment of the original design

Keep the separation of the study interface, conversation state, Anki tool
registry and Python-enforced proposal permissions. Reuse a mature agent runtime
where it performs well, but do not turn that preference into a ban on API access.
Billing/authentication method, model provider and agent runtime are three
separate choices; a runtime may support both subscription and API credentials.

Prefer one capable runtime serving both access paths when that reduces setup and
maintenance without degrading skills, tool use or conversation controls. Evaluate
a direct API adapter if it materially improves installation or user experience;
that option is reopened, not preselected. Preserve the current AnkiWeb-shippable
packaging constraints when comparing embedded libraries and subprocesses.

## Product behavior

### Connect an account; keep studying

The primary setup choice is **Use a subscription** or **Use an API key**, followed
by provider/account and model choices supported by that route. Advanced runtime
selection belongs in settings only if users need it. Preserve an explicit demo
option and clear setup/recovery instructions. Start by qualifying one working
subscription route and one working API route, rather than promising every
provider or subscription plan.

Subscription access uses provider-supported authentication/integration. API
access uses the user's selected API account. Show the active access method,
provider and model; disclose API billing when choosing that route. Never silently
switch from subscription to API on quota exhaustion, nor silently substitute a
provider. Show account/limit failures with a clear recovery action.

Keep secrets out of chat, transcripts and ordinary add-on configuration. Prefer
existing harness/OS credential stores or an intentional credential-source
interface; local development uses ordinary environment variables resolved at the
process boundary. Do not introduce a second public login flow or secret-handling
implementation without evaluating the installation and maintenance cost.

Use the same current-card context, tools, proposal previews, permissions, stop,
history and user-owned guidance across routes. Populate model/effort choices
from supported capabilities. Ordinary users should not have to understand MCP,
CLI protocols or agent runtimes to ask a study question.

Persist provider, runtime, access-method identifier and native session identity
with each chat, never credentials. Model changes can preserve a session when
supported. Cross-runtime/provider/account changes need explicit continuation
behavior: preserve the original and label a summary-based handoff honestly.
The visible transcript and model history must agree after branching/retry.

Declare capabilities for resume, head/historical branching, interruption,
images, skills, approvals and usage. Do not fake parity, but require essential
study behavior from both access routes before calling them product-complete.

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

1. **Architecture comparison — completed with bounded evidence.** The
   [report and reproducible probe](RUNTIME_COMPARISON_2026-11-02.md) cover setup,
   documented access methods and 28 passing synthetic compatibility checks on
   actual Claude, Codex and Pi processes. Recommend Claude Code plus Codex
   app-server first; defer Pi and a CWYC-owned API loop. No paid model calls or
   real-account trials were run. Authenticated subscription/API qualification,
   images, skill execution and study quality remain release criteria below.
2. **One complete subscription path and one complete API path.** Build simple
   connection setup and normalized events over shared Anki tools/proposals.
   Qualify current-card explanation, prerequisite search and reviewed note
   repair end to end. Demonstrate quota/auth failure, stop, resume and teardown.
   No silent fallback to paid API use. Establish tested minimum versions.
3. **Fresh skills and dependable sessions.** Add session preparation before
   prewarming, stable turn/item IDs, access/runtime metadata and lineage with
   backward-compatible transcripts. Test new chat, history and background
   analysis. Preserve user edits and make the next session see current guidance.
4. **Branch and edit/retry.** Use supported runtime APIs at completed boundaries.
   Test exact model/UI prefix agreement, inactive copied proposals, no replayed
   writes, late events and restart recovery. Start with per-message actions and
   lineage links; do not build a tree viewer before actual use warrants it.
5. **Qualify the study experience.** Run the same explanation, prerequisite-search
   and note-repair scenarios on both routes. Assess correctness, unnecessary
   questions, lost context, proposal usefulness, response delay, cancellation
   and recovery. Dogfood before expanding provider/runtime breadth. Keep private
   transcripts outside Git. A connection that merely launches is not a release.

No schedule or model-spend budget is inferred. The authorized comparison used
isolated local synthetic model endpoints and a temporary pinned Pi installation;
it did not change global installations, accounts, credentials or billing. No
production adapter implementation is included.

## Agent configuration of Anki — implemented in source

`get_anki_settings` and `set_anki_settings` expose the current Anki profile's
custom sync URL, automatic sync on open/close, media sync, background media
interval and network timeout. Existing collection preference/deck-option tools
remain separate, as do CWYC's own settings and external MCP connections.

Every profile-setting write is a human-reviewed proposal with readable old/new
values, including in Full collection and Read-only collection modes. Apply and
undo use Anki's profile APIs and save mechanism. They verify the active profile,
check for later edits and refuse changes during sync/background operations.
Undo anyway can override a value conflict but never a different profile, active
sync, or account/destination protection. Setting changes do not start sync or
choose a full-upload/full-download direction.

Changing the destination requires signing out of sync in Anki Preferences first,
so the old server's credentials cannot be sent to a new destination. Account
secrets are never exposed; stored URLs with embedded credentials/query data are
excluded from agent-readable settings. New custom URLs use a trailing slash for
reverse-proxy compatibility. No live profile settings were changed during
implementation; validation uses synthetic profiles and disposable Anki.

## Evidence and bounded unknowns

- [Codex app-server](https://learn.chatgpt.com/docs/app-server) documents
  `thread/fork`, turn streaming, interruption and `skills/list` force reload.
  The current history API is `thread/revert`; `thread/rollback` is removed.
  Revert affects conversation history, not file changes. Our edit/retry design
  still prefers preserving the original by branching. Exact installed-version
  schemas, streaming, native MCP, historical fork and interruption passed the
  synthetic probe; full permission mapping remains integration work.
- [Claude CLI reference](https://code.claude.com/docs/en/cli-reference) documents
  `--resume --fork-session`. This establishes head branching, not arbitrary
  historical boundaries in the current subprocess adapter. Verify a supported
  interface before enabling historical editing on Claude. The new-process head
  fork passed the comparison probe; historical Claude boundaries remain open.
- [Pi RPC](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc.md)
  and [session commands](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc-commands.md)
  document a persistent subprocess and native fork/clone operations.
  [Pi MCP](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/mcp.md)
  documents native transports and project/user configuration, superseding the
  July "no built-in MCP" assumption for current upstream.
- The subsequent isolated comparison exercised Claude Code 2.1.316, Codex
  0.159.0 and temporary Pi 1.4.0. The global Pi 0.80.3 was unchanged. Current
  upstream docs are not proof of capability in an arbitrary installed version.

- [OpenAI authentication](https://learn.chatgpt.com/docs/auth) documents subscription
  and API-key access for local Codex clients. This supports evaluating one runtime
  for both paths; it does not establish that every CWYC feature works unchanged
  under each mode or that other providers offer identical subscription access.
- [Anki self-hosted sync](https://docs.ankiweb.net/manual/sync-server) documents
  setting a custom server URL in Preferences. In the inspected Anki source,
  `ProfileManager.custom_sync_url()` / `set_custom_sync_url()` serve this setting;
  the new profile-setting tools wrap those native APIs separately from collection
  preferences.
