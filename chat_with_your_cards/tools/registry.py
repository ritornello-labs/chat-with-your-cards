"""Backend-neutral tool registry.

Tools are plain functions with JSON-schema signatures. The MCP server
(CLI backends) and the future in-process BYOK tool loop are thin
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
    def config(self) -> dict[str, Any]: ...  # live add-on config

    @property
    def learning(self) -> Any: ...  # learning.LearningStore or None


ToolFunc = Callable[[ToolContext, dict[str, Any]], Any]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    func: ToolFunc
    writes: bool = False
    trusted_only: bool = False  # advertised only in trusted-writes mode


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
        return spec.func(ctx, args)
