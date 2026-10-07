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
        # every profile open beside this pane, so each has a card
        present = patch.dict(panel.PRESENT, {a["name"]: 1 for a in panel.ap.AGENTS}, clear=True)
        present.start()
        self.addCleanup(present.stop)
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
        view = panel.View([a for a in panel.ap.AGENTS if a["name"] in panel.ap.FETCHERS])
        self.select_theme(view, "brand")
        return view

    def test_brand_outer_border_is_gray_animated_and_theme_name_is_temporary(self):
        view = self.view()
        self.select_theme(view, "brand")
        lines = panel.render(view, 80, 30, 1.5)
        ring = panel.ap.border_ring(panel.BRAND_FRAME, 1.5, 78, 28)
        for y, (left, _, right) in enumerate(lines[1:-1], 1):
            self.assertEqual(left, ring[(y, 0)] + panel.RESET)
            self.assertEqual(right, ring[(y, 79)] + panel.RESET)
            for red, green, blue in re.findall(r"\x1b\[38;2;(\d+);(\d+);(\d+)m", left + right):
                self.assertEqual(red, green)
                self.assertLessEqual(abs(int(blue) - int(red)), 8)
        later = panel.render(view, 80, 30, 2.5)
        self.assertNotEqual([line[0] for line in lines[1:-1]], [line[0] for line in later[1:-1]])
        if hasattr(panel, "preferences"):
            panel.preferences.put(panel.ANIMATE.key, False)
        else:
            view.anim = False
        still = panel.render(view, 80, 30, 1.5)
        later = panel.render(view, 80, 30, 2.5)
        self.assertEqual([line[0] for line in still[1:-1]], [line[0] for line in later[1:-1]])
        self.select_theme(view, "brand")
        self.assertNotIn("brand", ANSI.sub("", lines[0]))
        with patch.object(panel.time, "time", return_value=100):
            self.select_theme(view, "aurora")
            view.toast_until = 102.5
            self.assertIn("aurora", ANSI.sub("", panel.render(view, 80, 30, 1.5)[0]))
        with patch.object(panel.time, "time", return_value=103):
            self.assertNotIn("aurora", ANSI.sub("", panel.render(view, 80, 30, 1.5)[0]))
        self.assertNotEqual(panel.render(view, 80, 30, 1.5)[1][0], lines[1][0])

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

    def test_provider_positions_stay_fixed_across_activity_and_data_changes(self):
        view = self.view()
        names = [a["name"] for a in view.agents]

        def positions(cols, rows):
            lines = panel.render(view, cols, rows, 1.5)
            plain = [ANSI.sub("", "".join(line) if isinstance(line, tuple) else line)
                     for line in lines]
            return {name: next((y, line.index(name[:4])) for y, line in enumerate(plain)
                               if name[:4] in line) for name in names}

        states = [{}, *({name: 1} for name in names), {name: 2 for name in names}, {}]
        for cols, rows in ((80, 28), (120, 30), (60, 24), (200, 8), (40, 12), (30, 7), (18, 5), (8, 5)):
            panel.WORKING.clear()
            expected = positions(cols, rows)
            for state in states:
                panel.WORKING.clear()
                panel.WORKING.update(state)
                self.assertEqual(positions(cols, rows), expected, (cols, rows, state))
            entry = panel.USAGE.pop(names[0])
            self.assertEqual(positions(cols, rows), expected, (cols, rows, "no data"))
            panel.USAGE[names[0]] = dict(entry, windows=entry["windows"][:1])
            self.assertEqual(positions(cols, rows), expected, (cols, rows, "one limit"))
            panel.USAGE[names[0]] = entry

    def test_themes_change_provider_borders_and_rainbow_title_and_percentage_colors(self):
        view = self.view()
        now = time.time()
        panel.WORKING.update({a["name"]: 1 for a in view.agents})

        def cells(text):
            color, out = None, []
            for token in re.findall(r"\x1b\[[0-9;]*m|[^\x1b]", text):
                if token == panel.RESET:
                    color = None
                elif token.startswith("\x1b["):
                    if token.startswith("\x1b[38;2;"):
                        color = tuple(map(int, token[7:-1].split(";")))
                else:
                    out.append((token, color))
            return out

        for i, agent in enumerate(view.agents):
            self.select_theme(view, "brand")
            baseline = panel.working_card(view, i, 50, 1.5, now)
            title = cells(baseline[1])[4:4 + len(agent["name"])]
            compact = cells(panel.compact_row(view, i, 80, 1.5, now))[3:3 + len(agent["name"])]
            for theme in panel.THEMES[1:]:
                self.select_theme(view, theme["name"])
                themed = panel.working_card(view, i, 50, 1.5, now)
                if theme["stops"] != agent.get("gradient"):
                    self.assertNotEqual(themed[0], baseline[0], (agent["name"], theme["name"]))
                row = panel.compact_row(view, i, 80, 1.5, now)
                if theme["name"] == "rainbow":
                    expected = [(ch, panel.ap.rainbow_rgb(k / 11 - 1.5 * 0.24, 0.2))
                                for k, ch in enumerate(agent["name"])]
                    self.assertEqual(cells(themed[1])[4:4 + len(agent["name"])], expected)
                    self.assertEqual(cells(row)[3:3 + len(agent["name"])], expected)
                else:
                    self.assertEqual(cells(themed[1])[4:4 + len(agent["name"])], title)
                    self.assertEqual(cells(row)[3:3 + len(agent["name"])], compact)
                dot_color = panel.WHITE if theme["name"] == "rainbow" else tuple(agent["color"])
                self.assertEqual(cells(themed[1])[2][1], dot_color)
                self.assertEqual(cells(row)[1][1], dot_color)
                self.assertNotEqual(cells(row)[0][1], cells(panel.compact_row(view, i, 80, 2.5, now))[0][1])
            for anim in (False, True):
                panel.preferences.put(panel.ANIMATE.key, anim)
                for workers in (0, 1):
                    panel.WORKING[agent["name"]] = workers
                    for stale in (False, True):
                        entry = panel.USAGE[agent["name"]]
                        entry["fetched"] = now - (1800 if stale else 0)
                        entry["windows"][1]["pct"] = 85
                        for name in ("brand", "rainbow"):
                            panel.preferences.put(panel.THEME.key, name)
                            rendered = panel._card(view, i, 50, 1.5, now, entry, False, workers)
                            rendered.append(panel.compact_row(view, i, 80, 1.5, now))
                            dot_color = panel.WHITE if name == "rainbow" else tuple(agent["color"])
                            self.assertEqual(cells(rendered[0])[1][1], dot_color)
                            self.assertEqual(cells(rendered[-1])[1][1], dot_color)
                            percentages = []
                            for line in rendered:
                                drawn = cells(line)
                                plain = "".join(ch for ch, _ in drawn)
                                for match in re.finditer(r"\d+%", plain):
                                    percentages.append([color for _, color in drawn[match.start():match.end()]])
                            self.assertEqual(len(percentages), 4)
                            if name == "rainbow":
                                self.assertTrue(all(color == panel.WHITE for pct in percentages for color in pct))
                            else:
                                self.assertEqual(percentages[1], [panel.mix(panel.CONCERN, panel.SOFT, 0.5)
                                                                 if stale else panel.CONCERN] * 3)
                                self.assertEqual(percentages[3], [panel.CONCERN] * 3)


if __name__ == "__main__":
    unittest.main()
