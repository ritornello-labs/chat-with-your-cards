"""Backend-neutral tool registry.

Tools are plain functions with JSON-schema signatures. The MCP server
(CLI backends) are thin
adapters over this one registry (DESIGN.md section 5). No aqt imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol


class ToolContext(Protocol):
    """What tool implementations may touch. Satisfied by the add-on glue."""

    @property
    def col(self) -> Any: ...  # anki.collection.Collection

    @property
    def stats(self) -> dict[str, Any] | None: ...  # cached stats or None

    @property
    def proposals(self) -> Any: ...  # proposals.ProposalManager

    @property
    def grading(self) -> Any: ...  # grading.GradingManager

    @property
    def config(self) -> dict[str, Any]: ...  # live add-on config

    @property
    def learning(self) -> Any: ...  # learning.LearningStore or None

    @property
    def deferral(self) -> Any: ...  # deferral.DeferralManager or None

    def push_ui(self, payload: dict[str, Any]) -> None: ...  # record + push to the dock


ToolFunc = Callable[[ToolContext, dict[str, Any]], Any]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    func: ToolFunc
    writes: bool = False
    trusted_only: bool = False  # advertised only in direct-write collection modes
    # Accepted but deliberately NOT advertised in input_schema - aliases kept
    # working for compatibility without inviting their use (propose_note_edit
    # takes `fields` as an alias for `field_changes`). Anything here is exempt
    # from the unknown-argument check; everything else must be in the schema.
    extra_args: frozenset[str] = frozenset()
    # Runs on Anki's COLLECTION worker behind a progress dialog instead of the
    # Qt main thread (#13). FSRS optimization is seconds-to-minutes of pure
    # compute; on the main thread it freezes Anki and blows execute_tool's 15s
    # marshalling timeout, and moving it off Anki's single collection thread
    # entirely is forbidden (SAFETY.md hazard 19: a stray thread touching
    # mw.col poisons the backend mutex). QueryOp is exactly that worker.
    long_running: bool = False
    # Shown in the progress dialog while a long_running tool computes.
    progress_label: str = "Working…"


class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"duplicate tool: {spec.name}")
        self._specs[spec.name] = spec

    def specs(
        self, *, include_writes: bool = True, include_trusted: bool = False
    ) -> list[ToolSpec]:
        return [
            spec
            for spec in self._specs.values()
            if (include_writes or not spec.writes)
            and (include_trusted or not spec.trusted_only)
        ]

    def call(self, ctx: ToolContext, name: str, args: dict[str, Any]) -> Any:
        spec = self._specs.get(name)
        if spec is None:
            raise KeyError(f"unknown tool: {name}")
        check_args(spec, args)
        return spec.func(ctx, args)


def check_args(spec: ToolSpec, args: dict[str, Any]) -> None:
    """Enforce the schema's `required` and property list before dispatch.

    We advertise `required` to the model and, until now, never checked it: the
    args dict went straight to the handler, so a wrong argument name surfaced
    as whatever exception the handler happened to hit. `get_note_type` called
    with `{"type": "0 Cloze"}` died on `args["name"]`, and a KeyError's message
    is *only the key it could not find* - so the entire error the agent got
    back was the string `'name'`. It could not tell a missing argument from an
    unknown note type from an internal bug, and recovered by guessing again
    (dogfood 2026-07-27).

    Unknown keys are rejected rather than ignored for the same reason a
    misnamed `fields` was rejected locally in proposals.py: a silently dropped
    argument makes the tool answer a question nobody asked. That check now
    lives here, once, instead of per-tool.

    Types are deliberately NOT checked. Handlers coerce (`int(args["note_id"])`
    accepts "123"), and tightening that here would reject calls that work today
    for no gain in safety.
    """
    schema = spec.input_schema or {}
    properties = schema.get("properties") or {}
    allowed = set(properties) | set(spec.extra_args)
    unknown = sorted(set(args) - allowed)
    missing = [key for key in schema.get("required") or [] if key not in args]
    if not unknown and not missing:
        return

    # Both halves in ONE message. Reported separately, the `{"type": ...}`
    # call would have been told only that `type` is unknown - leaving the
    # caller to notice on its own that the argument it meant is still missing.
    problems = []
    if missing:
        problems.append(f"missing required argument(s) {missing}")
    if unknown:
        problems.append(f"unknown argument(s) {unknown}{_did_you_mean(unknown, allowed)}")
    got = f"[{', '.join(repr(k) for k in sorted(args))}]" if args else "nothing"
    raise ValueError(
        f"{spec.name}: {'; '.join(problems)}. "
        f"Received {got}; valid arguments: {', '.join(sorted(properties)) or '(none)'}."
    )


def _did_you_mean(unknown: list[str], allowed: set[str]) -> str:
    """Near-miss hints for a genuine typo (`max_char` -> `max_chars`). A wrong
    *concept* (`type` for `name`) is nowhere near, which is why the caller
    always prints the full valid list rather than relying on this."""
    from ..search_terms import suggest

    hints = []
    for key in unknown:
        best = suggest(key, sorted(allowed))
        if best:
            hints.append(f"{key} -> {best[0]}")
    return f" (did you mean {'; '.join(hints)}?)" if hints else ""
