import re
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import usage_panel as panel

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class UsagePanelTests(unittest.TestCase):
    def setUp(self):
        now = time.time()
        self.saved = dict(panel.USAGE), dict(panel.WORKING), panel.HERDR_OK
        panel.USAGE.clear()
        panel.USAGE.update({
            "Claude": {"windows": [{"label": "5h", "pct": 63, "resets": now + 3600},
                                   {"label": "wk", "pct": 18, "resets": now + 5 * 86400}], "fetched": now},
            "Kiro": {"windows": [{"label": "mo", "pct": 4, "resets": now + 20 * 86400}], "fetched": now - 3000},
        })
        panel.WORKING.clear()
        panel.WORKING["Claude"] = 1

    def tearDown(self):
        panel.USAGE.clear()
        panel.USAGE.update(self.saved[0])
        panel.WORKING.clear()
        panel.WORKING.update(self.saved[1])
        panel.HERDR_OK = self.saved[2]

    def view(self):
        agents = [a for a in panel.ap.AGENTS if a["name"] in panel.ap.FETCHERS]
        with patch.object(panel, "load_pref", lambda name, default: default):
            return panel.View(agents)

    def test_every_pane_size_fits_without_wrapping(self):
        view = self.view()
        for theme in range(len(panel.THEMES)):
            view.theme = theme
            for cols, rows in ((200, 8), (120, 30), (60, 24), (40, 12), (30, 7), (18, 3), (8, 1)):
                lines = panel.render(view, cols, rows, 1.5)
                self.assertLessEqual(len(lines), rows)
                for line in lines:
                    text = "".join(line) if isinstance(line, tuple) else line
                    self.assertLessEqual(len(ANSI.sub("", text)), cols, (cols, rows, text))

    def test_wide_short_panes_put_cards_side_by_side(self):
        self.assertEqual(panel.plan(5, 156, 6)[0], 3)
        self.assertEqual(panel.plan(5, 56, 22)[0], 1)
        self.assertIsNone(panel.plan(5, 30, 6))

    def test_pace_projects_running_out_before_reset(self):
        now = time.time()
        # 60% used one hour into a five-hour window: out long before the reset
        frac, out = panel.pace({"label": "5h", "pct": 60, "resets": now + 4 * 3600}, now)
        self.assertAlmostEqual(frac, 0.2, places=2)
        self.assertIsNotNone(out)
        self.assertLess(out, now + 4 * 3600)
        # 10% used halfway through: on track
        self.assertIsNone(panel.pace({"label": "5h", "pct": 10, "resets": now + 2.5 * 3600}, now)[1])

    def test_herdr_agent_names_map_to_providers(self):
        self.assertEqual(panel.provider_of("claude"), "Claude")
        self.assertEqual(panel.provider_of("codex"), "Codex")
        self.assertEqual(panel.provider_of("agy"), "Antigravity")
        self.assertIsNone(panel.provider_of(None))
        self.assertIsNone(panel.provider_of("bash"))

    def test_working_agents_are_read_more_often(self):
        panel.HERDR_OK = True
        working = panel.interval("Claude")
        panel.WORKING.clear()
        self.assertLess(working, panel.interval("Claude"))


if __name__ == "__main__":
    unittest.main()
