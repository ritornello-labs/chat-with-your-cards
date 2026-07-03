"""Backend-neutral collection tools (DESIGN.md section 5)."""

from .collection import build_registry, extract_field_prefixes
from .registry import ToolContext, ToolRegistry, ToolSpec

__all__ = [
    "ToolContext",
    "ToolRegistry",
    "ToolSpec",
    "build_registry",
    "extract_field_prefixes",
]
