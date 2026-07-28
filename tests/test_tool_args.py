"""ToolRegistry argument validation.

The bug: `get_note_type` was called with `{"type": "0 Cloze"}` (the argument is
`name`), nothing checked it, and the handler's `args["name"]` raised a KeyError
whose entire message is the key it could not find - so the agent got back the
string `'name'` and nothing else. It could not tell a wrong argument from an
unknown note type from an internal bug (dogfood 2026-07-27).
"""

from __future__ import annotations

import inspect
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chat_with_your_cards import proposals as proposals_mod  # noqa: E402
from chat_with_your_cards.tools import build_registry  # noqa: E402
from chat_with_your_cards.tools.registry import (  # noqa: E402
    ToolRegistry,
    ToolSpec,
    check_args,
)

SPECS = {spec.name: spec for spec in build_registry().specs(include_trusted=True)}


class ArgumentCheckTests(unittest.TestCase):
    def test_the_reported_call_explains_itself(self) -> None:
        with self.assertRaises(ValueError) as caught:
            check_args(SPECS["get_note_type"], {"type": "0 Cloze"})
        message = str(caught.exception)
        # Everything the bare KeyError failed to say: which tool, what is
        # missing, what was sent instead, and what it could have sent.
        self.assertIn("get_note_type", message)
        self.assertIn("'name'", message)
        self.assertIn("'type'", message)
        self.assertIn("max_chars", message)

    def test_missing_and_unknown_are_reported_together(self) -> None:
        """Reported separately, the call above would learn only that `type` is
        unknown - not that the argument it meant is still missing."""
        message = ""
        try:
            check_args(SPECS["get_note_type"], {"type": "x"})
        except ValueError as exc:
            message = str(exc)
        self.assertIn("missing required", message)
        self.assertIn("unknown argument", message)

    def test_typo_gets_a_near_miss_hint(self) -> None:
        with self.assertRaises(ValueError) as caught:
            check_args(SPECS["find_cards"], {"query": "x", "detial": "count"})
        self.assertIn("did you mean", str(caught.exception))
        self.assertIn("detail", str(caught.exception))

    def test_no_arguments_at_all(self) -> None:
        with self.assertRaises(ValueError) as caught:
            check_args(SPECS["get_note_type"], {})
        self.assertIn("Received nothing", str(caught.exception))

    def test_valid_calls_are_untouched(self) -> None:
        check_args(SPECS["get_note_type"], {"name": "Basic"})
        check_args(SPECS["get_note_type"], {"name": "Basic", "max_chars": 50, "offset": 2})
        check_args(SPECS["find_cards"], {"query": "deck:x"})
        # Optional-only schemas accept an empty call.
        check_args(SPECS["list_note_types"], {})

    def test_unadvertised_alias_still_works(self) -> None:
        """propose_note_edit accepts `fields` for `field_changes`. The alias is
        kept working without advertising it, so the model sees one name."""
        check_args(SPECS["propose_note_edit"], {"note_id": 1, "fields": {"Back": "x"}})
        self.assertNotIn(
            "fields", SPECS["propose_note_edit"].input_schema["properties"]
        )

    def test_types_are_not_enforced(self) -> None:
        """Handlers coerce (`int(args["note_id"])` takes "123"); tightening
        that here would reject calls that work today for no gain."""
        check_args(SPECS["get_note"], {"note_id": "123"})

    def test_registry_call_checks_before_dispatch(self) -> None:
        called: list[dict] = []
        registry = ToolRegistry()
        registry.register(
            ToolSpec(
                "demo",
                "",
                {"type": "object", "properties": {"a": {}}, "required": ["a"]},
                lambda ctx, args: called.append(args),
            )
        )
        with self.assertRaises(ValueError):
            registry.call(None, "demo", {"b": 1})  # type: ignore[arg-type]
        self.assertEqual([], called, "handler ran despite invalid arguments")
        registry.call(None, "demo", {"a": 1})  # type: ignore[arg-type]
        self.assertEqual([{"a": 1}], called)


class SchemaCompletenessTests(unittest.TestCase):
    """Rejecting unknown arguments is only safe while every key a handler
    actually reads is declared (or listed in `extra_args`). An undeclared key
    would now be refused instead of silently dropped - louder, but still
    wrong."""

    KEY_RE = re.compile(r'(?<![\w.])args(?:\.get\(|\[)\s*"([^"]+)"')
    DELEGATE_RE = re.compile(r"ctx\.\w+\.(\w+)\(args")

    def _keys_read(self, spec: ToolSpec) -> set[str]:
        source = inspect.getsource(spec.func)
        keys = set(self.KEY_RE.findall(source))
        # Most write tools are one-liners delegating to the manager, where the
        # real reads happen; follow that single hop.
        for method in self.DELEGATE_RE.findall(source):
            target = getattr(proposals_mod.ProposalManager, method, None)
            if target is not None:
                keys |= set(self.KEY_RE.findall(inspect.getsource(target)))
        return keys

    def test_every_key_a_handler_reads_is_accepted(self) -> None:
        for name, spec in SPECS.items():
            accepted = set(spec.input_schema.get("properties") or {}) | set(
                spec.extra_args
            )
            with self.subTest(tool=name):
                self.assertEqual(
                    set(),
                    self._keys_read(spec) - accepted,
                    f"{name} reads arguments its schema does not accept; add them "
                    "to `properties` (or to `extra_args` if deliberately hidden)",
                )


if __name__ == "__main__":
    unittest.main()
