"""BYOK API-key resolution (DESIGN.md backend strategy, 2026-07-05).

The add-on never implements its own API loop; keys are handed to the
agent harness (Claude Code / Codex / Pi) through its environment. Two
sources, in priority order:

1. `anthropic_api_key_op` - a 1Password reference (`op://vault/item/field`)
   resolved at spawn time via `op read`; the secret never touches disk.
2. `anthropic_api_key` - pasted directly into the add-on config
   (explicitly the less-secure option; stored in Anki's plain-text
   add-on config).

Empty both = use the harness's own login (subscription OAuth etc.).
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Any

OP_TIMEOUT_S = 10

# env var per provider; Codex/Pi reuse the same convention later.
ENV_BY_KEY = {
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "openai_api_key": "OPENAI_API_KEY",
}


def _read_op_reference(ref: str) -> tuple[str | None, str | None]:
    """Resolve an op:// reference; returns (secret, error)."""
    if not ref.startswith("op://"):
        return None, f"not an op:// reference: {ref!r}"
    op = shutil.which("op")
    if op is None:
        return None, "1Password CLI (op) not found on PATH"
    try:
        result = subprocess.run(
            [op, "read", ref],
            capture_output=True,
            text=True,
            timeout=OP_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return None, "op read timed out (is 1Password unlocked?)"
    if result.returncode != 0:
        return None, f"op read failed: {result.stderr.strip()[:200]}"
    secret = result.stdout.strip()
    return (secret, None) if secret else (None, "op read returned nothing")


def resolve_agent_env(config: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    """Build the extra environment for the agent subprocess.

    Returns (env vars, human-readable problems). Problems are surfaced as
    chat notices rather than raised: a broken key config should degrade to
    the harness's own login, not kill the chat.
    """
    env: dict[str, str] = {}
    problems: list[str] = []
    for key_name, env_name in ENV_BY_KEY.items():
        op_ref = str(config.get(f"{key_name}_op", "") or "").strip()
        plain = str(config.get(key_name, "") or "").strip()
        if op_ref:
            secret, error = _read_op_reference(op_ref)
            if secret:
                env[env_name] = secret
                continue
            problems.append(f"{key_name}_op: {error}")
        if plain:
            env[env_name] = plain
    return env, problems
