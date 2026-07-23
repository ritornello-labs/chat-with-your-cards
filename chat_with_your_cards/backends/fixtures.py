"""Canned chat scripts for the ScriptedBackend.

Scripts are plain data so they can be validated in unit tests and edited
without touching backend logic. Each script is a list of steps:

- {"kind": "text", "markdown": str} - streamed to the UI as small deltas
- {"kind": "thinking", "estimated_tokens": [int, ...]} - a quiet
  reasoning phase: one empty-text ThinkingDelta per list entry, its
  estimated_tokens growing, mirroring the real Claude CLI's redacted-text
  shape (DESIGN.md section 9) so both UIs' rotating "Thinking..."
  indicator gets exercised by the scripted demo backend too
- {"kind": "tool", "tool": str, "summary": str, "result": str,
   "ok": bool, "duration_ms": int} - rendered as a tool-call chip
- {"kind": "propose", "proposal_kind": "create", "payload": {...}} -
  routed through the real ProposalManager (validation, proposal card,
  accept path), so demos and smoke tests cover the genuine write flow
"""

from __future__ import annotations

from typing import Any

Step = dict[str, Any]

DEFAULT_SCRIPT: list[Step] = [
    {
        "kind": "text",
        "markdown": (
            "This card is about the **epsilon-delta definition of a limit** - the formal "
            "way to pin down what \"approaches\" means.\n\n"
            "The idea in one sentence: you can make `f(x)` land as close to `L` as anyone "
            "demands, just by keeping `x` close enough to `a`.\n\n"
            "```text\n"
            "for every epsilon > 0 there is a delta > 0 such that\n"
            "0 < |x - a| < delta  implies  |f(x) - L| < epsilon\n"
            "```\n\n"
            "A useful mental model:\n\n"
            "1. Your opponent picks a tolerance (epsilon).\n"
            "2. You answer with a window around `a` (delta).\n"
            "3. If you can always answer, the limit is `L`.\n\n"
            "Want me to find related cards in this deck?"
        ),
    },
]

PUBLIC_EXPLAIN_SCRIPT: list[Step] = [
    {
        "kind": "text",
        "markdown": (
            "The difference is **when delta gets chosen**.\n\n"
            "- With ordinary continuity, you first choose a point `x`, then you may "
            "pick a delta that works near that point.\n"
            "- With uniform continuity, you must pick **one delta before anyone "
            "chooses the point**. That same window has to work everywhere.\n\n"
            "A useful picture is a landscape viewed through a fixed-width frame. "
            "Pointwise continuity lets you resize the frame whenever you move. "
            "Uniform continuity asks whether one frame width works across the whole "
            "landscape.\n\n"
            "Compactness matters because it lets finitely many local windows cover "
            "the space; the smallest of their deltas is still positive and works "
            "globally."
        ),
    },
]

TOOL_SCRIPT: list[Step] = [
    {
        "kind": "thinking",
        "estimated_tokens": [40, 95, 160],
    },
    {
        "kind": "text",
        "markdown": "Let me look for related cards in your collection first.\n\n",
    },
    {
        "kind": "tool",
        # Real backends emit MCP tool names (mcp__anki__*); the UI maps these
        # to friendly labels and hides bare/internal names, so the fixture
        # must use the real shape.
        "tool": "mcp__anki__search_notes",
        "summary": '{"query": "deck:current \\"limit\\""}',
        "result": "12 notes",
        "ok": True,
        "duration_ms": 900,
    },
    {
        "kind": "text",
        "markdown": (
            "Found **12 notes** touching on limits. The closest ones:\n\n"
            "- *Analysis: define continuity* - continuity is a limit statement in disguise\n"
            "- *Analysis: limit laws* - the algebra you use once limits exist\n"
            "- *Analysis: one-sided limits* - the left/right refinement\n\n"
            "The continuity card is probably the best follow-up to review together with "
            "this one, since it reuses the epsilon-delta template verbatim."
        ),
    },
]

PREREQUISITE_SCRIPT: list[Step] = [
    {
        "kind": "thinking",
        "estimated_tokens": [55, 120, 210],
    },
    {
        "kind": "text",
        "markdown": "I’ll trace the concepts this card depends on in your collection.\n\n",
    },
    {
        "kind": "tool",
        "tool": "mcp__anki__search_notes",
        "summary": (
            '{"query": "deck:\\"CWYC Demo::*\\" '
            '(tag:continuity OR tag:compactness)"}'
        ),
        "result": "4 notes",
        "ok": True,
        "duration_ms": 760,
    },
    {
        "kind": "text",
        "markdown": (
            "The missing bridge is **compactness**. I’d review these in order:\n\n"
            "1. *Continuity at a point: the epsilon–delta game*\n"
            "2. *Open covers and finite subcovers*\n"
            "3. *Heine–Cantor: continuous on compact implies uniformly continuous*\n\n"
            "Your current card asks why one delta can work everywhere. The third card "
            "answers exactly that: compactness turns all the local neighborhoods into "
            "a finite subcover, so their finitely many deltas have a positive minimum."
        ),
    },
]

LONG_SCRIPT: list[Step] = [
    {
        "kind": "text",
        "markdown": (
            "Here is the longer story, in parts.\n\n"
            "## Where the definition comes from\n\n"
            "Nineteenth-century analysis kept running into paradoxes because \"gets closer "
            "and closer\" was doing unsupervised work. Cauchy and Weierstrass replaced the "
            "moving picture with a static guarantee: a challenge-response game between "
            "tolerances.\n\n"
            "## Why the order of quantifiers matters\n\n"
            "`for every epsilon, there exists delta` is the whole content. Swap them and "
            "you get a much weaker (wrong) statement: one delta that works for every "
            "epsilon would force `f` to be locally constant.\n\n"
            "## How it shows up elsewhere\n\n"
            "- **Continuity**: same statement with `L = f(a)` and the puncture removed.\n"
            "- **Convergence of sequences**: delta becomes a threshold index `N`.\n"
            "- **Uniform continuity**: delta must work for all points at once - the "
            "quantifier moves out one level.\n"
            "- **Metric spaces**: absolute values become distances; nothing else changes.\n\n"
            "## A worked example\n\n"
            "To show `lim x->3 of 2x = 6`: given epsilon, pick `delta = epsilon / 2`. "
            "Then `0 < |x - 3| < delta` gives `|2x - 6| = 2|x - 3| < 2 * (epsilon/2) = "
            "epsilon`. The pattern - massage `|f(x) - L|` until `|x - a|` appears, then "
            "solve for delta - covers most textbook exercises.\n\n"
            "## What to memorize vs. derive\n\n"
            "Memorize the quantifier skeleton cold; derive everything else. If you can "
            "recite the skeleton and explain the game metaphor, the variations (one-sided, "
            "at infinity, uniform) are small edits rather than new facts."
        ),
    },
]

PROPOSE_SCRIPT: list[Step] = [
    {
        "kind": "text",
        "markdown": (
            "That gap is worth its own card - the quantifier order is exactly what "
            "trips people up. Here is a proposal:\n\n"
        ),
    },
    {
        "kind": "propose",
        "proposal_kind": "create",
        "payload": {
            "note_type": "Basic",
            "deck": "Default",
            "tags": ["analysis"],
            "fields": {
                "Front": "Analysis: why does the quantifier order in the "
                "epsilon-delta definition matter?",
                "Back": "Because <i>for every &epsilon; there exists &delta;</i> lets "
                "&delta; depend on &epsilon;. Swapping them demands one &delta; for "
                "all &epsilon;, which would force f to be locally constant.",
            },
            "rationale": "You said the quantifier order was the confusing part; "
            "this isolates it as a single recall step.",
        },
    },
    {
        "kind": "text",
        "markdown": (
            "I kept the front in your usual `Analysis:` prefix style. "
            "Accept it as-is, edit the fields first, or reject it."
        ),
    },
]

PUBLIC_PROPOSE_SCRIPT: list[Step] = [
    {
        "kind": "text",
        "markdown": (
            "Your confusion is about the **quantifier order**, so I’d isolate that "
            "instead of making the current card longer. Here is a focused companion card:\n\n"
        ),
    },
    {
        "kind": "propose",
        "proposal_kind": "create",
        "payload": {
            "note_type": "Basic",
            "deck": "CWYC Demo::Current",
            "tags": ["analysis", "continuity", "ai-proposed"],
            "fields": {
                "Front": "Uniform continuity: which quantifier moves?",
                "Back": (
                    "The same δ must work for every point in the domain. "
                    "Pointwise continuity may choose a different δ at "
                    "each point."
                ),
            },
            "rationale": (
                "This separates the quantifier change from the compactness proof, "
                "making each card test one idea."
            ),
        },
    },
    {
        "kind": "text",
        "markdown": (
            "Nothing has been written yet. You can edit either field, inspect the deck "
            "and tags, then accept or reject the proposal."
        ),
    },
]

SCRIPTS: dict[str, list[Step]] = {
    "default": DEFAULT_SCRIPT,
    "public_explain": PUBLIC_EXPLAIN_SCRIPT,
    "tool": TOOL_SCRIPT,
    "prerequisite": PREREQUISITE_SCRIPT,
    "long": LONG_SCRIPT,
    "propose": PROPOSE_SCRIPT,
    "public_propose": PUBLIC_PROPOSE_SCRIPT,
}


def select_script(user_text: str) -> list[Step]:
    """Pick a canned script from keywords in the user message (deterministic)."""
    lowered = user_text.lower()
    if "plain language" in lowered:
        return PUBLIC_EXPLAIN_SCRIPT
    if "turn my confusion" in lowered:
        return PUBLIC_PROPOSE_SCRIPT
    if "prerequisite" in lowered or "before this" in lowered:
        return PREREQUISITE_SCRIPT
    if "propose" in lowered or "create a note" in lowered or "make a card" in lowered:
        return PROPOSE_SCRIPT
    if "tool" in lowered:
        return TOOL_SCRIPT
    if "long" in lowered:
        return LONG_SCRIPT
    return DEFAULT_SCRIPT
