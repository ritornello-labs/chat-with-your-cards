"""Canned chat scripts for the ScriptedBackend.

Scripts are plain data so they can be validated in unit tests and edited
without touching backend logic. Each script is a list of steps:

- {"kind": "text", "markdown": str} - streamed to the UI as small deltas
- {"kind": "tool", "tool": str, "summary": str, "result": str,
   "ok": bool, "duration_ms": int} - rendered as a tool-call chip
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

TOOL_SCRIPT: list[Step] = [
    {
        "kind": "text",
        "markdown": "Let me look for related cards in your collection first.\n\n",
    },
    {
        "kind": "tool",
        "tool": "search_notes",
        "summary": 'deck:current "limit"',
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

SCRIPTS: dict[str, list[Step]] = {
    "default": DEFAULT_SCRIPT,
    "tool": TOOL_SCRIPT,
    "long": LONG_SCRIPT,
}


def select_script(user_text: str) -> list[Step]:
    """Pick a canned script from keywords in the user message (deterministic)."""
    lowered = user_text.lower()
    if "tool" in lowered:
        return TOOL_SCRIPT
    if "long" in lowered:
        return LONG_SCRIPT
    return DEFAULT_SCRIPT
