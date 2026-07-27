# Backend compliance: how CWYC talks to AI vendors

CWYC's first backend runs on top of a locally installed CLI agent (Claude
Code). That raises an obvious question: is it acceptable for a third-party
add-on to use an AI vendor's subscription this way? This file records the
design invariants that keep the answer yes. Code and tests cite these as
"COMPLIANCE.md rule N" — the numbering is stable.

## The distinction that is the whole game

**Credential extraction (never acceptable).** A non-vendor program reads the
subscription's stored OAuth credential and calls the vendor's API directly,
impersonating the official client. Anthropic's published guidance
(`code.claude.com/docs/en/legal-and-compliance`) is explicit that OAuth
credentials are for native Anthropic applications and that third parties
should use API-key authentication; routing requests through a user's
subscription credentials from a non-native app is prohibited, and tools that
did this have been blocked.

**Spawning the official binary (what CWYC does).** Run the real,
user-installed, unmodified `claude` binary that the user themselves logged
into (e.g. `claude -p`), and read its output. The request originates from the
genuine client with its genuine credentials and telemetry. This is ordinary
scripted use of a CLI the user already has, on their own machine, for their
own account — the same category as invoking it from a shell script or editor
integration.

## Design rules (binding)

1. **Spawn-only, forever.** CWYC spawns the user's own official CLI binary.
   It never reads stored credentials (`~/.claude/.credentials.json`, the
   macOS Keychain), never calls the vendor API with a subscription
   credential, and never exports an extracted token as an API key. A direct
   API backend, when it lands, is BYOK: the user supplies their own API key,
   used openly as an API key.
2. **Never proxy or pool.** One user, their own login, their own machine.
   CWYC never routes multiple users through one account and never relays
   requests between machines.
3. **Keep the injected system prompt lean, generic, and bounded.** Unbounded
   app content does not belong in `--append-system-prompt`: the collection
   overview is fetched on demand through a tool, and the user's note
   conventions are materialized as a skill the harness discovers itself
   (`skills.py:materialize_conventions_agent_skill`) with a one-line pointer
   in the prompt. What remains in `context.py:build_system_prompt` is
   bounded and roughly constant-size, guarded by
   `tests/test_context_and_stats.py::ContextTest::test_system_prompt_length_ceiling_worst_case`
   (under 4,000 chars in the worst case). This keeps per-session token
   overhead predictable and keeps CWYC's footprint that of a well-behaved
   harness guest rather than a prompt-stuffing wrapper.
4. **Own branding.** CWYC is its own product with its own name, look, and
   iconography — no vendor logos, no look-alike branding, no implication of
   endorsement.

## Cross-vendor note

A subprocess-of-the-official-CLI design is the conservative common
denominator across vendors: it is the least-privileged integration shape any
of them offers, which is why the backend abstraction treats it as the floor
(Codex adapter planned on the same shape).
