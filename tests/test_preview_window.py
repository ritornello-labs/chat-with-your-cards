"""Large preview window (#2): the pure pieces (face flattening, srcdoc)
plus render_for_window's routing. Qt itself is only provable in the GUI
smoke, which opens the real dialog."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chat_with_your_cards.preview_window import (  # noqa: E402
    build_preview_html,
    preview_faces,
)
from tests.test_proposals import CREATE_ARGS, make_manager  # noqa: E402


class BuildHtmlTests(unittest.TestCase):
    def test_embeds_css_content_and_night_classes(self) -> None:
        html = build_preview_html("<b>front</b>", ".card { color: red; }", night=True)
        self.assertIn(".card { color: red; }", html)
        self.assertIn("<b>front</b>", html)
        self.assertIn('class="card nightMode night_mode"', html)
        self.assertIn("#2c2c2c", html)

    def test_day_mode_and_empty_content(self) -> None:
        html = build_preview_html(None, None, night=False)
        self.assertIn('class="card"', html)
        self.assertNotIn("nightMode", html)
        self.assertIn("(nothing rendered)", html)


class PreviewFacesTests(unittest.TestCase):
    def test_edit_shape_uses_before_after_answer_sides(self) -> None:
        faces = preview_faces(
            {
                "before": {"question": "q", "answer": "old a", "css": "c1"},
                "after": {"question": "q", "answer": "new a", "css": "c2"},
            }
        )
        self.assertEqual(
            [("Before", "old a", "c1"), ("After", "new a", "c2")], faces
        )

    def test_create_shape_uses_front_back(self) -> None:
        faces = preview_faces(
            {"before": None, "after": {"question": "q", "answer": "a", "css": "c"}}
        )
        self.assertEqual([("Front", "q", "c"), ("Back", "a", "c")], faces)

    def test_empty_previews_yield_no_faces(self) -> None:
        self.assertEqual([], preview_faces({"before": None, "after": None}))


class RenderForWindowTests(unittest.TestCase):
    def test_create_renders_and_draft_overrides(self) -> None:
        manager, _col, _pushed = make_manager()
        result = manager.submit_create(
            {**CREATE_ARGS, "fields": {"Front": "Original front", "Back": "b"}}
        )
        previews = manager.render_for_window(result["proposal_id"])
        self.assertIn("Original front", previews["after"]["question"])
        drafted = manager.render_for_window(
            result["proposal_id"], {"Front": "Drafted front", "Back": "b"}
        )
        self.assertIn("Drafted front", drafted["after"]["question"])

    def test_unknown_and_bulk_proposals_return_none(self) -> None:
        manager, col, _pushed = make_manager()
        self.assertIsNone(manager.render_for_window("nope"))
        col.decks._add("Focus")
        bulk = manager.submit_set_deck_limits({"deck": "Focus", "new_limit_today": 0})
        self.assertIsNone(manager.render_for_window(bulk["proposal_id"]))


if __name__ == "__main__":
    unittest.main()
