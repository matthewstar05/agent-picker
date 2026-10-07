import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import agent_picker
import preferences
import usage_panel as panel

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class UsagePanelTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for patcher in (patch.object(preferences, "PATH", Path(folder.name) / "preferences.json"),
                        patch.object(preferences, "_values", {})):
            patcher.start()
            self.addCleanup(patcher.stop)
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
        present = patch.dict(panel.PRESENT, {a["name"]: 1 for a in panel.ap.AGENTS}, clear=True)
        present.start()
        self.addCleanup(present.stop)

    def tearDown(self):
        panel.USAGE.clear()
        panel.USAGE.update(self.saved[0])
        panel.WORKING.clear()
        panel.WORKING.update(self.saved[1])
        panel.HERDR_OK = self.saved[2]

    def view(self):
        return panel.View([a for a in panel.ap.AGENTS if a["name"] in panel.ap.FETCHERS])

    def test_every_pane_size_fits_without_wrapping(self):
        view = self.view()
        for theme in panel.THEMES:
            preferences.put(panel.THEME.key, theme["name"])
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

    def test_limits_turn_red_only_when_high_with_the_reset_far_off(self):
        now = time.time()
        hour, day = 3600, 86400
        cases = [("5h", 75, 3 * hour, True), ("5h", 75, 1 * hour, False), ("5h", 60, 4 * hour, False),
                 ("wk", 70, 3 * day, True), ("wk", 90, 1 * day, False),
                 ("mo", 80, 10 * day, True), ("cr", 95, 4 * day, False)]
        for label, pct, left, expected in cases:
            w = {"label": label, "pct": pct, "resets": now + left}
            self.assertEqual(panel.concerning(w, now), expected, (label, pct, left))

    def test_several_instances_animate_without_breaking_layout(self):
        panel.WORKING["Claude"] = 3
        view = self.view()
        for cols in (40, 64, 120):
            lines = panel.render(view, cols, 24, 2.3)
            text = "".join("".join(l) if isinstance(l, tuple) else l for l in lines)
            self.assertIn("3 working", ANSI.sub("", text))
            for line in lines:
                line = "".join(line) if isinstance(line, tuple) else line
                self.assertLessEqual(len(ANSI.sub("", line)), cols)

    def test_only_agents_beside_this_pane_get_cards(self):
        panel.PRESENT.clear()
        view = self.view()
        lines = panel.render(view, 60, 20, 1.0)
        text = ANSI.sub("", "".join("".join(l) if isinstance(l, tuple) else l for l in lines))
        self.assertIn("no agents in", text)
        self.assertNotIn("Claude", text)
        panel.PRESENT["Codex"] = 1
        lines = panel.render(view, 60, 20, 1.0)
        text = ANSI.sub("", "".join("".join(l) if isinstance(l, tuple) else l for l in lines))
        self.assertIn("Codex", text)
        self.assertNotIn("Claude", text)
        for cols, rows in ((20, 3), (8, 1)):
            panel.PRESENT.clear()
            self.assertLessEqual(len(panel.render(view, cols, rows, 1.0)), rows)

    def test_working_card_keeps_its_full_border_in_a_tight_pane(self):
        # Four agents in 19 rows: too short for every card to reserve a border
        # (4 x 5 = 20), but the one working card still gets both of its lids.
        names = ["Claude", "Codex", "Antigravity", "Cursor"]
        panel.PRESENT.clear()
        panel.PRESENT.update({n: 1 for n in names})
        panel.WORKING.clear()
        panel.WORKING["Claude"] = 1
        now = time.time()
        for n in names:
            panel.USAGE[n] = {"windows": [{"label": "5h", "pct": 20, "resets": now + 9000}], "fetched": now}
        view = panel.View([a for a in panel.ap.AGENTS if a["name"] in names])
        plain = [ANSI.sub("", "".join(l) if isinstance(l, tuple) else l) for l in panel.render(view, 46, 21, 1.0)]
        claude = next(y for y, line in enumerate(plain) if "Claude" in line)
        self.assertIn("╭", plain[claude - 1])
        bottom = next(y for y in range(claude, len(plain)) if "╰" in plain[y][:4])
        self.assertLess(bottom, next(y for y, line in enumerate(plain) if "Codex" in line))

    def test_tight_pane_spaces_cards_evenly_around_a_working_border(self):
        names = ["Claude", "Codex", "Antigravity", "Cursor", "Kiro"]
        panel.PRESENT.clear()
        panel.PRESENT.update({n: 1 for n in names})
        panel.WORKING.clear()
        panel.WORKING["Antigravity"] = 1
        now = time.time()
        for n in names:
            panel.USAGE[n] = {"windows": [{"label": "5h", "pct": 20, "resets": now + 9000},
                                          {"label": "wk", "pct": 5, "resets": now + 400000}], "fetched": now}
        view = panel.View([a for a in panel.ap.AGENTS if a["name"] in names])
        plain = [ANSI.sub("", "".join(l) if isinstance(l, tuple) else l) for l in panel.render(view, 44, 25, 1.0)]
        rows = [next(y for y, line in enumerate(plain) if n in line) for n in names]
        self.assertEqual(len({b - a for a, b in zip(rows, rows[1:])}), 1, rows)
        working = rows[2]
        self.assertIn("╭", plain[working - 1])
        self.assertIn("╰", plain[working + 3])

    def test_cli_without_a_usage_reader_says_so(self):
        opencode = next(a for a in panel.ap.AGENTS if a["name"] == "OpenCode")
        panel.PRESENT.clear()
        panel.PRESENT["OpenCode"] = 1
        view = panel.View([opencode])
        text = ANSI.sub("", "".join("".join(l) if isinstance(l, tuple) else l
                                    for l in panel.render(view, 60, 12, 1.0)))
        self.assertIn("no usage reader", text)

    def test_processes_map_to_profiles_through_launchers(self):
        self.assertEqual(panel.from_argv(["node", "/usr/lib/node_modules/opencode/bin/opencode.js"]), "OpenCode")
        self.assertEqual(panel.from_argv(["/opt/homebrew/bin/qwen", "--yolo"]), "Qwen Code")
        self.assertEqual(panel.from_argv(["kiro-cli-chat"]), "Kiro")
        self.assertIsNone(panel.from_argv(["zsh"]))
        procs = [(1, 0, ["zsh"]), (2, 1, ["claude"]), (3, 2, ["claude", "mcp"]), (4, 1, ["codex"]),
                 (5, 1, ["vim"]), (6, 5, ["codex"])]
        self.assertEqual(panel.agents_under([1], procs), {"Claude": 1, "Codex": 2})

    def test_herdr_agent_names_map_to_providers(self):
        self.assertEqual(panel.provider_of("claude"), "Claude")
        self.assertEqual(panel.provider_of("codex"), "Codex")
        self.assertEqual(panel.provider_of("agy"), "Antigravity")
        self.assertEqual(panel.provider_of("qwen"), "Qwen Code")
        self.assertEqual(panel.provider_of("opencode"), "OpenCode")
        self.assertIsNone(panel.provider_of(None))
        self.assertIsNone(panel.provider_of("bash"))

    def test_working_agents_are_read_more_often(self):
        panel.HERDR_OK = True
        with patch.dict(panel.BUSY, {"Claude": 1}):
            working = panel.interval("Claude")
        self.assertLess(working, panel.interval("Claude"))


    def test_usage_keys_switch_screens_and_themes(self):
        view = self.view()
        self.assertEqual(panel.handle(view, "t", []), None)
        self.assertEqual(preferences.get(panel.THEME.key), panel.THEMES[1]["name"])
        panel.handle(view, "left", [])
        self.assertEqual(preferences.get(panel.THEME.key), "brand")
        self.assertEqual(panel.handle(view, "u", []), "agents")
        self.assertEqual(panel.handle(view, "esc", []), "agents")
        self.assertEqual(panel.handle(view, "s", []), "settings")
        self.assertEqual(panel.handle(view, "q", []), "quit")


class SettingsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for patcher in (patch.object(preferences, "PATH", Path(folder.name) / "preferences.json"),
                        patch.object(preferences, "_values", None)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_settings_list_every_registered_setting_and_persist(self):
        keys = [s.key for s in preferences.SETTINGS]
        self.assertEqual(keys[:3], ["start", "usage.theme", "usage.animate"])
        menu = preferences.Menu()
        self.assertEqual(preferences.get("start"), "agents")
        menu.handle("right")
        self.assertEqual(preferences.get("start"), "usage")
        menu.handle("down")
        menu.handle("down")
        menu.handle("right")
        self.assertIs(preferences.get("usage.animate"), not panel.ANIMATE.default)
        self.assertEqual(menu.handle("esc"), "close")
        preferences._values = None   # read back from disk
        self.assertEqual(preferences.get("start"), "usage")

    def test_unknown_saved_value_falls_back_to_default(self):
        preferences.put("usage.theme", "no-such-theme")
        self.assertEqual(preferences.get("usage.theme"), panel.THEME.default)

    def test_settings_screen_fits_every_size(self):
        menu = preferences.Menu()
        for index in range(len(preferences.SETTINGS)):
            menu.index = index
            for cols, rows in ((120, 40), (60, 20), (40, 8), (30, 5), (20, 3), (8, 1)):
                lines = agent_picker.render_settings(menu, cols, rows, 0.5, panel.BRAND_FRAME)
                self.assertLessEqual(len(lines), rows)
                for line in lines:
                    self.assertLessEqual(agent_picker.visible_len(line), cols, (cols, rows, line))


if __name__ == "__main__":
    unittest.main()
