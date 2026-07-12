"""Doctor-lite: report the agent setup, diagnose the obvious breakages.

The add-on deliberately does NOT install agents (decision 2026-07-05);
this panel reports what is present, its version, and links to the
official install pages. A binary that exists but cannot answer
--version is reported as broken (the probed volta-codex failure mode).

aqt-free: callers run gather_report on a background thread (version
subprocesses can take a second each) and push the result to the UI.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

VERSION_TIMEOUT_S = 10

HARNESSES = [
    ("Claude Code", "claude", "https://claude.com/claude-code"),
    ("Codex CLI (future backend)", "codex", "https://developers.openai.com/codex/cli/"),
    ("Pi (future backend)", "pi", "https://pi.dev/"),
    ("1Password CLI (for op:// keys)", "op", "https://developer.1password.com/docs/cli/"),
]


def _binary_row(label: str, path: str | None, link: str) -> dict[str, Any]:
    if path is None:
        return {"label": label, "status": "missing", "detail": "not found on PATH", "link": link}
    try:
        result = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=VERSION_TIMEOUT_S
        )
    except Exception as exc:
        return {
            "label": label,
            "status": "broken",
            "detail": f"{path} exists but --version failed: {exc}",
            "link": link,
        }
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        return {
            "label": label,
            "status": "broken",
            "detail": f"{path}: " + (detail[0][:120] if detail else "--version failed"),
            "link": link,
        }
    version = (result.stdout or result.stderr).strip().splitlines()
    return {
        "label": label,
        "status": "ok",
        "detail": (version[0][:80] if version else "installed") + f" — {path}",
        "link": link,
    }


def gather_report(
    *,
    config: dict[str, Any],
    backend_kind: str,
    mcp_url: str | None,
    agent_home: Path,
    find_claude: Callable[[str], str | None],
    learning: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for label, binary, link in HARNESSES:
        if binary == "claude":
            # GUI apps don't inherit the shell PATH; use the same lookup the
            # backend uses so the doctor reports what the chat will get.
            path = find_claude(str(config.get("claude_cli_path", "")))
        else:
            path = shutil.which(binary)
        rows.append(_binary_row(label, path, link))

    rows.append(
        {
            "label": "Active backend",
            "status": "ok" if backend_kind == "claude" else "warn",
            "detail": backend_kind
            + ("" if backend_kind == "claude" else " (demo backend - no real AI)"),
        }
    )
    rows.append(
        {
            "label": "Collection tool server (MCP)",
            "status": "ok" if mcp_url else "warn",
            "detail": mcp_url or "starts with the first real chat",
        }
    )
    skills_root = agent_home / ".claude" / "skills"
    factory_skills = (
        "anki-card-authoring",
        "anki-curriculum-design",
        "anki-curriculum-delivery",
        "skill-maintenance",
    )
    present_skills = [
        name for name in factory_skills if (skills_root / name / "SKILL.md").exists()
    ]
    # The doctor panel is ~360px wide; an absolute path wraps over six lines.
    # Show the path relative to the add-on package when it lives inside it
    # (the normal case), falling back to the absolute path for exotic setups.
    try:
        skills_disp = str(skills_root.relative_to(Path(__file__).resolve().parent))
    except ValueError:
        skills_disp = str(skills_root)
    rows.append(
        {
            "label": "Built-in Anki skills",
            "status": "ok" if len(present_skills) == len(factory_skills) else "warn",
            "detail": (
                f"{len(present_skills)}/{len(factory_skills)} materialized under {skills_disp}"
                if present_skills
                else "not materialized yet"
            ),
        }
    )
    if learning is not None:
        kb = learning.get("bytes", 0) / 1024
        rows.append(
            {
                "label": "Edit-pattern learning",
                "status": "ok",
                "detail": f"{learning.get('snapshots', 0)} AI-written note(s) "
                f"watched, {learning.get('pending', 0)} pending observation(s), "
                f"{kb:.0f} KB on disk (uncapped by design - grows only with "
                "AI-written notes)",
            }
        )
    key_source = "harness login (no API key configured)"
    if str(config.get("anthropic_api_key_op", "")).strip():
        key_source = "1Password reference (op://)"
    elif str(config.get("anthropic_api_key", "")).strip():
        key_source = "pasted key in add-on config"
    rows.append({"label": "Anthropic billing", "status": "ok", "detail": key_source})
    rows.append(
        {
            "label": "Permission mode",
            "status": "ok",
            "detail": str(config.get("permission_mode", "default"))
            + (", web access on" if config.get("web_access", True) else ", web access off"),
        }
    )
    return rows
