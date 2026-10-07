import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import usage_panel as panel

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class UsageBorderTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        if hasattr(panel, "preferences"):
            for patcher in (patch.object(panel.preferences, "PATH", Path(folder.name) / "preferences.json"),
                            patch.object(panel.preferences, "_values", {})):
                patcher.start()
                self.addCleanup(patcher.stop)
        self.saved = dict(panel.USAGE), dict(panel.WORKING)
        panel.USAGE.clear()
        panel.WORKING.clear()
        now = time.time()
        panel.USAGE.update({a["name"]: {"windows": [{"label": "5h", "pct": 15, "resets": now + 10800},
                                                    {"label": "wk", "pct": 6, "resets": now + 400000}],
                                        "fetched": now} for a in panel.ap.AGENTS})

    def tearDown(self):
        panel.USAGE.clear()
        panel.USAGE.update(self.saved[0])
        panel.WORKING.clear()
        panel.WORKING.update(self.saved[1])

    def select_theme(self, view, name):
        if hasattr(panel, "preferences"):
            panel.preferences.put(panel.THEME.key, name)
            panel.preferences.put(panel.ANIMATE.key, True)
        else:
            view.theme = panel.theme_index(name)
            view.anim = True

    def view(self):
        view = panel.View(panel.ap.AGENTS)
        self.select_theme(view, "brand")
        return view

    def test_brand_outer_border_is_white_and_theme_name_is_temporary(self):
        view = self.view()
        self.select_theme(view, "brand")
        lines = panel.render(view, 80, 30, 1.5)
        white = panel.ap.fg(panel.WHITE)
        for left, _, right in lines[1:-1]:
            self.assertEqual(left, white + "│" + panel.RESET)
            self.assertEqual(right, white + "│" + panel.RESET)
        for glyph in re.findall(r"(\x1b\[[0-9;]*m[╭╮─])", lines[0]):
            self.assertTrue(glyph.startswith(white), glyph)
        self.assertNotIn("brand", ANSI.sub("", lines[0]))
        with patch.object(panel.time, "time", return_value=100):
            self.select_theme(view, "aurora")
            view.toast_until = 102.5
            self.assertIn("aurora", ANSI.sub("", panel.render(view, 80, 30, 1.5)[0]))
        with patch.object(panel.time, "time", return_value=103):
            self.assertNotIn("aurora", ANSI.sub("", panel.render(view, 80, 30, 1.5)[0]))
        self.assertNotEqual(panel.render(view, 80, 30, 1.5)[1][0], white + "│" + panel.RESET)

    def test_working_cards_use_selector_border_and_unbox_when_idle(self):
        view = self.view()
        now = time.time()
        for i, agent in enumerate(view.agents):
            panel.WORKING[agent["name"]] = 1
            lines = panel.working_card(view, i, 40, 1.5, now)
            ring = panel.ap.border_ring(agent, 1.5, 38, 3)
            self.assertEqual(lines[0], "".join(ring[(0, c)] for c in range(40)) + panel.RESET)
            self.assertEqual(len(lines), 5)
            self.assertIn(agent["name"], ANSI.sub("", lines[1]))
            self.assertTrue(all(len(ANSI.sub("", line)) == 40 for line in lines))
        panel.WORKING.clear()
        lines = panel.render(view, 80, 30, 1.5)
        self.assertFalse(any("╭" in ANSI.sub("", line[1]) for line in lines[1:-1]))

    def test_all_working_providers_fit_short_and_narrow_panes(self):
        view = self.view()
        panel.WORKING.update({a["name"]: 1 for a in view.agents})
        for cols, rows in ((200, 8), (120, 30), (60, 24), (40, 12), (30, 7), (18, 5)):
            lines = panel.render(view, cols, rows, 1.5)
            plain = [ANSI.sub("", "".join(line) if isinstance(line, tuple) else line)
                     for line in lines]
            self.assertLessEqual(len(lines), rows)
            self.assertTrue(all(len(line) <= cols for line in plain), (cols, rows))
            if cols >= 40:
                self.assertTrue(all(any(a["name"][:4] in line for line in plain) for a in view.agents))
            self.assertTrue(any("│" in line for line in plain))


if __name__ == "__main__":
    unittest.main()
