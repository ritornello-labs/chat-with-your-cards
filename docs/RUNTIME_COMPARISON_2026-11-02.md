# CWYC runtime comparison — 2026-11-02

## Recommendation

Keep the shipped Claude Code adapter and add Codex app-server. Offer subscription
and API access through those official runtimes first. Defer Pi until additional
API providers justify a third runtime. Do not build a custom API agent loop now.

This is an engineering recommendation from documentation and isolated protocol
tests, not a user commitment to a release schedule or a completed backend rollout.
It supersedes treating Pi as a required next adapter. It does not exclude Pi:
Pi passed the controlled tests and is a credible multi-provider API option.

The distinction is **access method versus runtime**. Reusing an agent runtime
does not require excluding API keys. A direct API request can be simple, but
matching the existing agent's tools, skills, context management and conversation
recovery would require owning much more than HTTP streaming.

## Options

| Option | Subscription and API fit | Setup and integration | Skills/conversation controls | Decision |
|---|---|---|---|---|
| Unmodified Claude Code CLI | Native user subscription route; Anthropic API-key route through the same executable | Already integrated; API-mode configuration and credential isolation still need implementation | Real skill discovery, streaming, MCP execution, interrupt acknowledgement and a new-process head fork passed | Keep and extend |
| Codex app-server | Official local Codex supports ChatGPT sign-in and API-key access | New adapter, but a documented persistent protocol and native executable; no need to implement a second OpenAI agent loop | Discovery/reload, streaming, MCP execution, resume, head/historical fork and interruption passed | Add next |
| Pi RPC 1.4.0 | Broad API-provider support; OAuth availability is provider-specific and does not establish included subscription billing | Additional Node/CLI installation and adapter; requires a current supported version rather than the installed 0.80.3 | Discovery/refresh, streaming, native MCP execution, head/historical fork, interrupt and process-restart resume passed | Revisit for API breadth |
| CWYC-owned direct API loop | API keys can use provider APIs directly; subscription access needs an explicitly supported provider integration | Simplest potential end-user install, largest agent-runtime implementation/maintenance surface | CWYC would own tool loops, skill discovery/loading/execution, compaction, retries, cancellation, history and branching | Defer unless installation evidence changes the tradeoff |

### Why not make Pi the universal backend?

Pi's API breadth is useful, but the initial product requirement is subscription
plus API access, not every provider. The official routes can cover the first
OpenAI/Anthropic connections while preserving the shipped integration. Pi would
introduce another install, event vocabulary and capability/permission mapping
before a user needs that breadth. That additional cost is an engineering
judgment, not a measured runtime failure.

Subscription compatibility needs provider evidence. Anthropic's current guidance
distinguishes users signing in to its unmodified Claude Code binary from a third
party offering its own Claude-account login. Do not assume a Pi OAuth option
uses included Claude plan allowance or provides the same permitted integration.
OpenAI documents both local Codex access methods. Those facts favor keeping the
official subscription paths while evaluating API breadth separately.

### Permissions are not interchangeable

Keep Anki permission checks and proposals in Python for every backend. Do not
translate all runtimes' power settings into one misleading approval ladder.
Pi's upstream security documentation says its tools/extensions use the operating
system account's permissions; project trust is not an OS sandbox. Codex exposes
sandbox and approval policies. The current Claude adapter also uses explicit
tool deny rules. Disabling tools and providing an OS boundary are different
capabilities. This is a mapping requirement, not a reason to reject Pi.

## What was tested

The reusable [probe](../spikes/runtime-comparison/README.md) starts actual installed
executables with isolated home/config/session directories. An in-process Python
fixture serves synthetic Responses/Anthropic event streams and CWYC's real MCP
HTTP transport with a fake, read-only `search_notes` executor.

There were no real model-service requests, real credentials, account changes,
Anki launches or collection access. The [sanitized results](../spikes/runtime-comparison/results.json)
record 28 passing assertions. Fourteen final-fixture requests stayed on
loopback. The returned explanation is scripted: this does **not** compare model
quality, real-provider latency, account entitlement or production error rates.

| Runtime | Version | Observed passing checks |
|---|---|---|
| Claude Code | 2.1.316 | 6: completed turn, skill discovery, streamed events, native MCP call, interrupt acknowledgement, native head fork in a new process |
| Codex | 0.159.0 | 10: startup, discovery, completed turn, streamed text, head fork, native resume, historical-turn fork, native MCP call, forced skill reload, interrupted terminal state |
| Pi | 1.4.0 | 12: startup, discovery, streamed text, native history, head fork, historical fork, native MCP call, interrupt/idle state, empty new chat, metadata refresh on New session, restart/resume, skill refresh after restart |

These are adapter-specific compatibility assertions, not a leaderboard. Codex
resume was exercised in a running app-server. Codex historical forking was
checked after two completed turns: the fork retained only the first turn while
the original kept both. Pi restart/resume and Claude's
new-process head fork were exercised across processes. Arbitrary historical
branching in CWYC's Claude subprocess adapter was not qualified by this probe.

The Pi fixture installed exactly 1.4.0, published October 21. Latest 1.5.0 was
published on November 2 and was intentionally not selected. Installation used
Socket Firewall, a seven-day pnpm release-age gate, ignored install scripts, and
a committed fixture lockfile. The global Pi installation was not upgraded.

### Integration findings

- Resolve and test the actual executable in the add-on subprocess environment.
  A local shell launcher can depend on the user's home or a version-manager
  shim even when `command -v` succeeds. The probe resolved the underlying
  executable/Node path for isolated runs. This is setup evidence, not a model
  reliability result.
- Codex represents MCP tools as namespaces in Responses requests. Its adapter
  must preserve tool namespace and call identity rather than flattening names.
- Claude project skills require the appropriate settings source. The fixture
  enables project discovery and forces upfront MCP definitions to exercise a
  single synthetic tool; production tool-search behavior needs its own case.
- Branch boundaries are native entry/turn IDs, not UI message counts. Pi can
  retain an internal system item before the first visible user turn.
- All three engines already provide useful session machinery. Rebuilding that
  machinery merely to add an API key is not justified by this comparison.

## Product implementation direction

1. The user chooses an account/access route and model. Runtime names remain
   advanced details. Start with Claude and OpenAI connections, each offering
   subscription or API mode where supported.
2. Keep the four connection identities distinct: Claude subscription, Claude API,
   ChatGPT subscription, OpenAI API. Never silently switch billing routes when
   limits are reached, and never change the user's global CLI login to configure
   a different CWYC connection. Isolate API credentials/configuration at the
   child-process boundary using an intentional credential-source mechanism.
3. Add one normalized event/capability contract over the existing adapters.
   Preserve the same card context, skills, tool registry and proposal manager.
   Track runtime/native session, connection identity, stable turn IDs and lineage
   without putting credentials in transcripts.
4. Implement Codex as a small end-to-end slice: current-card explanation,
   prerequisite search, proposed edit, stop, resume, skills and history. Pin the
   tested protocol surface and detect unsupported installed versions.
5. Extend the current Claude route to API credentials without replacing its
   agent loop. Verify the effective authentication mode and expose API billing
   clearly. Native Claude subscription login remains in the unmodified client.
6. Implement branch/edit-and-retry using supported boundaries. Keep collection
   undo separate, copied proposals inactive, and original chats preserved.
7. Add Pi when a concrete provider need wins enough value to justify its install
   and maintenance. Reuse the passed RPC/MCP spike as the starting point.

## Required before claiming product parity

- Bounded authenticated trials of the chosen subscription and API routes, with
  provider/account billing and quota behavior verified. No billable API trial
  was run in this comparison.
- Native disposable-Anki integration of each new adapter, including proposal
  staleness, permission modes, process death, restart, large histories and late
  events. The synthetic runtime probe does not replace that test.
- Real explanation/search/note-repair examples, evaluated for usefulness,
  unnecessary questions, factual quality and time to a useful answer.
- Qualification of skill assets/scripts and external-tool power settings under
  each runtime's actual permission model, beyond the discovery tests here.

The tutor, dependency graphs and capstone gating remain deferred. No production
adapter, login change, live collection operation or AnkiWeb release is included.

## Primary sources

- [OpenAI authentication](https://learn.chatgpt.com/docs/auth): local Codex's
  subscription and API-key access methods.
- [Codex app-server](https://learn.chatgpt.com/docs/app-server): lifecycle,
  streaming, approvals, skill reload and branch operations. The installed
  executable's generated JSON schemas were checked as well.
- [Claude Code integration and authentication guidance](https://code.claude.com/docs/en/legal-and-compliance):
  unmodified client/user authentication versus third-party credential handling.
- [Claude programmatic interface](https://code.claude.com/docs/en/headless),
  [sessions](https://code.claude.com/docs/en/agent-sdk/sessions), and
  [environment variables](https://code.claude.com/docs/en/env-vars).
- Pi 1.4.0's versioned [MCP](https://github.com/earendil-works/pi/blob/v1.4.0/packages/coding-agent/docs/mcp.md),
  [providers](https://github.com/earendil-works/pi/blob/v1.4.0/packages/coding-agent/docs/providers.md),
  [RPC](https://github.com/earendil-works/pi/blob/v1.4.0/packages/coding-agent/docs/rpc-commands.md),
  and [security](https://github.com/earendil-works/pi/blob/v1.4.0/packages/coding-agent/docs/security.md)
  documentation, checked against the isolated installed package.
