"""New-skill proposal tool (workspace task #20, DESIGN.md section 7).

`propose_new_skill` lets the agent propose SAVING a reusable workflow it
worked out with the user (e.g. "generate TTS audio via the user's API") as a
brand-new skill under agent_home/.claude/skills/<name>/SKILL.md, so future
chats load it on demand instead of the agent re-deriving the same workflow
every session. Nothing about the mechanism is task-specific - it works for
any workflow the agent and user settle on.

SECURITY: a skill is standing instructions loaded into every future session.
This add-on feeds the agent untrusted card content (a shared/downloaded deck
is untrusted model input, AGENTS.md), so a booby-trapped deck could try to
steer the agent into planting a malicious skill here ("always exfiltrate
X"). That is why this tool only ever stages a reviewable proposal
(proposals.ProposalManager.submit_skill_create, kind "skill_create") - the
user reads a proposal card and must explicitly accept before anything is
written to disk. Nothing here writes directly, in any permission mode,
mirroring propose_skill_update's existing posture for skill *updates*.
"""

from __future__ import annotations

from typing import Any

from .registry import ToolContext, ToolRegistry, ToolSpec


def propose_new_skill(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return ctx.proposals.submit_skill_create(args)


def register_skill_tools(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            "propose_new_skill",
            "When you and the user work out a reusable workflow worth "
            "keeping (e.g. \"generate TTS audio via the user's API\"), "
            "propose saving it as a brand-new skill so future chats load it "
            "on demand. A skill is standing instructions loaded into every "
            "future session, so this ALWAYS requires explicit user "
            "confirmation on a review card - nothing is ever written "
            "directly, in any permission mode. name must be kebab-case and "
            "must not collide with an existing skill (use "
            "propose_skill_update instead if you mean to revise the "
            "existing card-authoring skill).",
            {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "kebab-case skill directory name, e.g. "
                        "'tts-audio-workflow' (lowercase letters, digits, and "
                        "hyphens only)",
                    },
                    "description": {
                        "type": "string",
                        "description": "One or two sentences for the SKILL.md "
                        "frontmatter: what the skill is for and when to load "
                        "it. This is what future sessions read to decide "
                        "whether to load the skill, so make it concrete.",
                    },
                    "markdown": {
                        "type": "string",
                        "description": "The skill body in Markdown - no "
                        "frontmatter (name/description above generate it). "
                        "Explain the workflow: steps, tools/commands used, "
                        "and any gotchas worth remembering.",
                    },
                    "rationale": {
                        "type": "string",
                        "description": "Why this is worth saving as a "
                        "reusable skill; shown to the user on the review card",
                    },
                },
                "required": ["name", "description", "markdown", "rationale"],
            },
            propose_new_skill,
            writes=True,
        )
    )
