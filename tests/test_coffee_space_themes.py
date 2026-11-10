"""Regression coverage for optional per-file colors in coffee-space themes."""

import re
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import fs_browser as fsb
import themes
from test_fs_browser import Base


class ThemeMetadataTests(unittest.TestCase):
    def test_file_color_accepts_hex_and_rgb_and_normalizes_to_tuple(self):
        for value in ("#e8bf68", [232, 191, 104]):
            with self.subTest(value=value):
                theme, error = themes._theme({"name": "galaxy", "stops": "rainbow",
                                              "file_color": value})
                self.assertIsNone(error)
                self.assertEqual(theme["file_color"], (232, 191, 104))

    def test_invalid_file_color_skips_entry_and_reports_reason(self):
        for value in ("#abc", "red", [256, 0, 0], [True, 0, 0], None):
            with self.subTest(value=value):
                theme, error = themes._theme({"name": "bad", "stops": [[1, 2, 3]],
                                              "file_color": value})
                self.assertIsNone(theme)
                self.assertIn("file_color", error)

    def test_merge_replaces_metadata_without_leaking_it_to_other_themes(self):
        built_in = [{"name": "galaxy", "stops": "rainbow", "file_color": (232, 191, 104)},
                    {"name": "latte", "stops": [[1, 2, 3]]}]
        replacement, error = themes._theme({"name": "galaxy", "stops": [[4, 5, 6]]})
        self.assertIsNone(error)
        local = [replacement]
        merged = themes._merge(built_in, local)
        self.assertEqual(merged[0], {"name": "galaxy", "stops": [(4, 5, 6)]})
        self.assertNotIn("file_color", merged[1])

    def test_new_builtin_theme_names_are_in_theme_menu_choices(self):
        required = {"latte", "solar", "galaxy", "rose"}
        self.assertTrue(required.issubset(themes.names()))
        menu = fsb.ThemeMenu("usage")
        self.assertTrue(required.issubset(menu.items))


class RenderedFilenameColorTests(Base):
    GALAXY = (232, 191, 104)
    ANSI = re.compile(r"\x1b\[([0-9;]*)m")

    @classmethod
    def _styled_text(cls, frame):
        out = []
        for row in frame:
            for _, text in row:
                fg = None
                at = 0
                for match in cls.ANSI.finditer(text):
                    out.extend((ch, fg) for ch in text[at:match.start()])
                    codes = [int(x) for x in match.group(1).split(";") if x]
                    if not codes or codes[0] == 0:
                        fg = None
                    for i, code in enumerate(codes):
                        if code == 38 and i + 4 < len(codes) and codes[i + 1] == 2:
                            fg = tuple(codes[i + 2:i + 5])
                    at = match.end()
                out.extend((ch, fg) for ch in text[at:])
        return out

    @classmethod
    def _assert_frame_label_color(cls, test, frame, label, expected, occurrence=0):
        styled = cls._styled_text(frame)
        chars = "".join(ch for ch, _ in styled)
        start = -1
        cursor = 0
        for _ in range(occurrence + 1):
            start = chars.find(label, cursor)
            if start < 0:
                break
            cursor = start + len(label)
        test.assertNotEqual(start, -1, f"{label!r} is absent from rendered frame")
        colors = [fg for _, fg in styled[start:start + len(label)]]
        test.assertTrue(all(color == expected for color in colors), (label, colors, expected))

    @staticmethod
    def _assert_grid_label_color(test, grid, label, expected):
        chars = "".join(cell[0] for row in grid for cell in row)
        start = chars.find(label)
        test.assertNotEqual(start, -1, f"{label!r} is absent from rendered grid")
        cells = [cell for row in grid for cell in row]
        colors = [cell[1] for cell in cells[start:start + len(label)]]
        test.assertEqual(colors, [expected] * len(label))

    def _frame_for(self, view, selected_name):
        app = self.app(view=view, hidden=True, theme="galaxy")
        app.focused = False  # hold the configured filename color steady in the highlight.
        app.entries = [e for e in app.entries if e.name in {"file2.txt", "notes.md", ".secret"}]
        app.index = next(i for i, e in enumerate(app.entries) if e.name == selected_name)
        if view == "tiny":
            return app.tiny_frame(48, 4, 0.4)
        return app.browse_cells(120, 12, 0.4)

    def test_icons_details_and_tiny_render_selected_and_unselected_file_color(self):
        self.assertEqual(themes.get("galaxy").get("file_color"), self.GALAXY)
        selected_color = fsb.ap.mix(self.GALAXY, fsb.WHITE, 0.18)
        for view in ("icons", "details", "tiny"):
            for selected, name in ((True, "file2.txt"), (False, "notes.md")):
                with self.subTest(view=view, selected=selected):
                    frame = self._frame_for(view, "file2.txt")
                    wanted = selected_color if selected else self.GALAXY
                    if view == "tiny":
                        self._assert_frame_label_color(self, frame, name, wanted)
                    else:
                        self._assert_grid_label_color(self, frame, name, wanted)

    def test_hidden_file_color_is_dimmed_in_each_rendered_view(self):
        self.assertEqual(themes.get("galaxy").get("file_color"), self.GALAXY)
        dimmed = fsb.ap.mix(self.GALAXY, fsb.DARK, 0.35)
        for view in ("icons", "details", "tiny"):
            with self.subTest(view=view):
                frame = self._frame_for(view, "file2.txt")
                if view == "tiny":
                    self._assert_frame_label_color(self, frame, ".secret", dimmed)
                else:
                    self._assert_grid_label_color(self, frame, ".secret", dimmed)

    def test_preview_header_and_info_name_use_galaxy_file_color(self):
        self.assertEqual(themes.get("galaxy").get("file_color"), self.GALAXY)
        app = self.app(theme="galaxy")
        entry = next(e for e in app.entries if e.name == "notes.md")
        app.preview = fsb.Preview(entry, "galaxy")
        app.preview.mode = "info"
        app.screen = "preview"
        frame = app.frame(120, 18, 0.4)
        self._assert_frame_label_color(self, frame, "notes.md", self.GALAXY)
        self._assert_frame_label_color(self, frame, "notes.md", fsb.ap.mix(self.GALAXY, fsb.WHITE, 0.18),
                                       occurrence=1)

    def test_brand_and_mono_allow_white_file_labels_while_vapor_is_tinted(self):
        for name in ("brand", "mono"):
            self.assertEqual(fsb.filename_color(name), fsb.WHITE)
            self.assertEqual(fsb.filename_color(name, selected=True), fsb.WHITE)
        self.assertNotEqual(fsb.filename_color("vapor"), fsb.WHITE)
