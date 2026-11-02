# Backend and conversation plan — 2026-11-02

Status: revised product requirements and implementation recommendation; not implemented.
The MCP settings manager is implemented in the current source PR. It is a human
connection-management screen, separate from the existing agent self-settings
and Anki preference tools. Dependency graphs and capstone gating remain deferred.
The long-recall tutor is important retained product direction, explicitly for
later; no tutor implementation is authorized in the current work.

## Requirements clarified after the initial plan

Support both subscription access and API-key access as first-class options.
Pi is neither a requirement nor a committed milestone. A harness, SDK or direct
API loop is an implementation choice to evaluate against the product needs.
This supersedes the August harness-only/API-exclusion decision and the earlier
November recommendation to build Pi as a required adapter.

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

1. **Bounded architecture comparison.** Test candidate runtimes against one
   subscription route and one API route using synthetic data and a separately
   agreed model-spend budget. Codex app-server is a strong candidate for an
   OpenAI route; current Claude Code remains the shipped baseline. Pi is an
   optional candidate if it improves the product. Compare supported auth,
   install burden, skills/tool execution, streaming, images, cancellation,
   history/branching and recovery. Reuse an existing loop when it passes; choose
   direct API integration if its UX benefit justifies owning that loop. Deliver
   a choice with evidence before committing to the runtime architecture.
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

No schedule or model-spend budget is inferred. This is planning only; no new
provider account, credential, billing change, model turn or runtime installation
is authorized by this clarification.

## Agent configuration of Anki — proposed, separate work

Current tools already cover deck options and an allowlisted set of collection
review/edit/scheduling preferences. The MCP manager configures the assistant's
external tool connections; `get_addon_settings`/`set_addon_settings` separately
cover ordinary CWYC preferences through reviewable proposals.

Profile/global Anki settings such as custom sync URL are not currently exposed
by those collection preference tools. A useful next family would inspect and
propose supported Anki settings with a plain old/new comparison and exact scope
(current profile versus collection versus application). Candidates include the
custom sync URL, auto-sync/media-sync preferences and network timeout; these are
recommendations, not implemented or newly committed scope.

For a sync-destination change, show the old/new destination and require review
because it determines where collection data goes. Use Anki's profile APIs and
persist through its supported mechanism; do not edit a database directly. Keep
account secrets out of model-visible reads. Changing a URL does not authorize
syncing, migrating accounts, or choosing full-upload/full-download direction.
Test persistence, staleness, scope and absence of implicit sync in a disposable
profile before exposing the tool. No live settings were changed in this review.

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

- [OpenAI authentication](https://learn.chatgpt.com/docs/auth) documents subscription
  and API-key access for local Codex clients. This supports evaluating one runtime
  for both paths; it does not establish that every CWYC feature works unchanged
  under each mode or that other providers offer identical subscription access.
- [Anki self-hosted sync](https://docs.ankiweb.net/manual/sync-server) documents
  setting a custom server URL in Preferences. In the inspected Anki source,
  `ProfileManager.custom_sync_url()` / `set_custom_sync_url()` serve this setting;
  CWYC's existing preference tools only wrap collection preferences.
