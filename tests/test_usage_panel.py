import io
import json
import os
import re
import sys
import tempfile
import time
import unittest
import urllib.error
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

    def test_every_framed_row_spans_the_pane(self):
        # the redraw puts each row's right edge at a fixed column, so a short row
        # (an incomplete last grid row) would leave last frame's cells frozen there
        view = self.view()
        view.peek_until = time.time() + 60
        panel.WORKING["Antigravity"] = 1
        for cols in range(26, 200, 7):
            for rows in range(6, 40, 3):
                for line in panel.render(view, cols, rows, 1.5):
                    if isinstance(line, tuple):
                        self.assertEqual(len(ANSI.sub("", "".join(line))), cols, (cols, rows))

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

    def test_working_card_uses_white_text_for_reset_timers(self):
        now = time.time()
        entry = {"windows": [{"label": "5h", "pct": 20, "resets": now + 7200}], "fetched": now}
        view = self.view()
        idle_card = panel._card(view, 0, 50, 0, now, entry, False, 0)
        idle_line = next(line for line in idle_card if "↻" in line)
        self.assertIn(panel.ap.fg(panel.SOFT), idle_line)
        self.assertNotIn(panel.ap.fg(panel.TEXT), idle_line)

        work_card = panel._card(view, 0, 50, 0, now, entry, False, 1)
        work_line = next(line for line in work_card if "↻" in line)
        self.assertIn(panel.ap.fg(panel.TEXT), work_line)
        self.assertNotIn(panel.ap.fg(panel.SOFT), work_line)

        entry_concerning = {"windows": [{"label": "5h", "pct": 80, "resets": now + 3 * 3600}], "fetched": now}
        concern_card = panel._card(view, 0, 50, 0, now, entry_concerning, False, 1)
        concern_line = next(line for line in concern_card if "↻" in line)
        self.assertIn(panel.ap.fg(panel.CONCERN), concern_line)

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

    def test_working_status_uses_a_fixed_brand_count_and_animated_theme_wave(self):
        def header(view, workers, tick):
            text = panel._card(view, 0, 50, tick, 2000, None, False, workers)[0]
            cells = []
            for codes, ch in re.findall(r"((?:\x1b\[[0-9;]*m)*)([^\x1b])", text):
                rgb = re.search(r"\x1b\[38;2;(\d+);(\d+);(\d+)m", codes)
                cells.append((ch, tuple(map(int, rgb.groups())) if rgb else None))
            plain = "".join(ch for ch, _ in cells)
            start = plain.index(f"{workers} working")
            self.assertTrue(plain[3 + len(view.agents[0]["name"]):start].isspace())
            return cells[start:start + len(str(workers))], cells[start + len(str(workers)) + 1:][:7]

        for agent in panel.ap.AGENTS[:5]:
            view = panel.View([agent])
            for theme in ("brand", "sunset", "rainbow"):
                preferences.put(panel.THEME.key, theme)
                expected = (panel.WHITE if theme == "rainbow" or agent.get("pastel") == "rainbow"
                            else tuple(agent.get("pastel") or agent["color"]))
                waves = []
                for workers in (1, 3, 12):
                    first_count, first_wave = header(view, workers, 0.4)
                    next_count, next_wave = header(view, workers, 0.8)
                    self.assertTrue(all(color == expected for _, color in first_count))
                    self.assertEqual(first_count, next_count)
                    self.assertNotEqual(first_wave, next_wave)
                    self.assertGreater(len({color for _, color in first_wave}), 1)
                    if theme == "rainbow":
                        for _, color in first_wave:
                            self.assertAlmostEqual(1 - min(color) / max(color), 0.2, places=2)
                    waves.append(first_wave)
                self.assertEqual(waves[0], waves[1])
                self.assertEqual(waves[1], waves[2])
                preferences.put(panel.ANIMATE.key, False)
                self.assertEqual(header(view, 1, 0.4), header(view, 1, 0.8))
                preferences.put(panel.ANIMATE.key, True)

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

    def test_open_pane_count_refreshes_cached_idle_cards(self):
        codex = next(a for a in panel.ap.AGENTS if a["name"] == "Codex")
        view = panel.View([codex])
        now = time.time()
        with patch.object(panel, "CARDS", {}):
            before = "".join(panel.card(view, 0, 40, 0, now))
            panel.PRESENT["Codex"] = 3
            after = "".join(panel.card(view, 0, 40, 0, now))
            panel.PRESENT["Codex"] = 1
            single = "".join(panel.card(view, 0, 40, 0, now))
            compact = panel.compact_row(view, 0, 32, 0, now)
        self.assertIn("Codex", ANSI.sub("", before))
        self.assertNotIn("(1)", ANSI.sub("", before))
        self.assertIn("Codex (3)", ANSI.sub("", after))
        for text in (single, compact):
            self.assertIn("Codex", ANSI.sub("", text))
            self.assertNotIn("(1)", ANSI.sub("", text))

    def test_pane_counts_keep_brand_color_and_regular_weight_in_both_layouts(self):
        for name in ("Codex", "Antigravity", "Cursor"):
            agent = next(a for a in panel.ap.AGENTS if a["name"] == name)
            panel.PRESENT[name] = 3
            panel.WORKING[name] = 1
            view = panel.View([agent])
            if agent.get("pastel") == "rainbow":
                suffix = "".join(panel.ap.fg(panel.ap.rainbow_rgb((len(name) + k) / 11, 0.2))
                                 + ch + panel.RESET for k, ch in enumerate(" (3)"))
            else:
                color = panel.ap.fg(agent.get("pastel") or agent["color"])
                suffix = "".join(color + ch + panel.RESET for ch in " (3)")
            for text in ("".join(panel.card(view, 0, 24, 0, time.time())),
                         panel.compact_row(view, 0, 32, 0, time.time())):
                self.assertIn(name + " (3)", ANSI.sub("", text))
                self.assertIn(suffix, re.sub(r"\x1b\[48;2;[0-9;]+m", "", text))

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

    def test_idle_cards_center_when_the_last_card_is_short(self):
        names = ["Claude", "Codex", "Antigravity", "Cursor", "Kiro"]
        panel.PRESENT.clear()
        panel.PRESENT.update({n: 1 for n in names})
        panel.WORKING.clear()
        now = time.time()
        for n in names:
            windows = [{"label": "5h", "pct": 20, "resets": now + 9000},
                       {"label": "wk", "pct": 5, "resets": now + 400000}]
            panel.USAGE[n] = {"windows": windows[:1] if n == "Kiro" else windows, "fetched": now}
        view = panel.View([a for a in panel.ap.AGENTS if a["name"] in names])
        plain = [ANSI.sub("", "".join(l) if isinstance(l, tuple) else l) for l in panel.render(view, 36, 22, 1.0)]
        shown = [y for y, line in enumerate(plain[1:-1]) if line.strip("│ ")]
        self.assertEqual(shown[0], 20 - 1 - shown[-1], plain)

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


class CardScopeTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for patcher in (patch.object(preferences, "PATH", Path(folder.name) / "preferences.json"),
                        patch.object(preferences, "_values", {}),
                        patch.dict(panel.PRESENT, clear=True), patch.dict(panel.WORKING, clear=True),
                        patch.dict(panel.SESSIONS, {s: {} for s in panel.SCOPE_ORDER}, clear=True)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.view = panel.View([a for a in panel.ap.AGENTS if a["name"] in panel.ap.FETCHERS])
        # Claude: 1 here, 2 in this workspace, 3 anywhere. Kiro: a team in another
        # tab of this workspace. Codex: only in another workspace.
        panel.SESSIONS.update({
            "tab": {"Claude": {"panes": 1, "working": 0}},
            "workspace": {"Claude": {"panes": 2, "working": 1}, "Kiro": {"panes": 5, "working": 3}},
            "all": {"Claude": {"panes": 3, "working": 2}, "Kiro": {"panes": 5, "working": 3},
                    "Codex": {"panes": 4, "working": 1}}})

    def names(self):
        return [a["name"] for a in self.view.present()]

    def test_cards_appear_for_this_tab_and_count_everywhere_by_default(self):
        panel.rescope()
        self.assertEqual(self.names(), ["Claude"])
        self.assertEqual(panel.PRESENT, {"Claude": 3})
        self.assertEqual(panel.WORKING, {"Claude": 2})
        self.assertEqual(panel.SCOPE, panel.SCOPES["tab"])

    def test_universal_display_reaches_this_workspace_or_everywhere(self):
        preferences.put(panel.UNIVERSAL.key, "workspace")
        panel.rescope()
        self.assertEqual(self.names(), ["Claude", "Kiro"])
        self.assertEqual(panel.SCOPE, panel.SCOPES["workspace"])
        preferences.put(panel.UNIVERSAL.key, True)
        panel.rescope()
        self.assertEqual(self.names(), ["Claude", "Codex", "Kiro"])
        self.assertEqual(panel.PRESENT, {"Claude": 3, "Kiro": 5, "Codex": 4})
        self.assertEqual(panel.WORKING, {"Claude": 2, "Kiro": 3, "Codex": 1})
        self.assertEqual(panel.SCOPE, panel.SCOPES["all"])

    def test_agent_count_setting_picks_everywhere_this_workspace_or_this_tab(self):
        for count, panes in (("all", 3), ("workspace", 2), ("tab", 1)):
            preferences.put(panel.COUNT.key, count)
            panel.rescope()
            self.assertEqual(panel.PRESENT, {"Claude": panes}, count)
            self.assertEqual(panel.WORKING, {"Claude": 2}, count)   # working follows the card setting

    def test_card_scopes_and_always_shown(self):
        preferences.put(panel.UNIVERSAL.key, True)
        preferences.put(panel.CARD_KEY + "Claude", "workspace")
        preferences.put(panel.CARD_KEY + "Codex", "workspace")
        preferences.put(panel.CARD_KEY + "Kiro", "tab")
        preferences.put(panel.CARD_KEY + "Cursor", "always")
        panel.rescope()
        self.assertEqual(panel.WORKING, {"Claude": 1})
        self.assertEqual(self.names(), ["Claude", "Cursor"])
        for name in panel.ap.FETCHERS:
            preferences.put(panel.CARD_KEY + name, "tab")
        panel.rescope()
        self.assertEqual(panel.SCOPE, panel.SCOPES["tab"])

    def test_holding_tab_shows_every_connected_provider_until_released(self):
        self.assertEqual(self.names(), [])
        panel.handle(self.view, "\t", [], releases=True)
        self.assertEqual(set(self.names()), set(panel.ap.FETCHERS))
        lines = panel.render(self.view, 80, 30, 1.0)
        self.assertIn("usage · every provider", ANSI.sub("", lines[0]))
        panel.handle(self.view, "release:\tx", [], releases=True)   # some other key's release
        self.assertTrue(self.view.peeking())
        panel.handle(self.view, "release:\t", [], releases=True)
        self.assertEqual(self.names(), [])

    def test_without_release_events_key_repeat_keeps_the_peek_open(self):
        with patch.object(panel.time, "time", return_value=1000.0):
            panel.handle(self.view, "\t", [])
        with patch.object(panel.time, "time", return_value=1000.5):   # before the first repeat
            self.assertTrue(self.view.peeking())
            panel.handle(self.view, "\t", [])
        with patch.object(panel.time, "time", return_value=1000.7):
            self.assertTrue(self.view.peeking())
        with patch.object(panel.time, "time", return_value=1000.8):   # repeats stopped: let go
            self.assertFalse(self.view.peeking())

    def test_herdr_counts_this_tab_this_workspace_and_every_workspace(self):
        panes = [{"pane_id": "w:p1", "tab_id": "w:t1", "workspace_id": "w"},
                 {"pane_id": "w:p2", "tab_id": "w:t1", "workspace_id": "w", "agent": "claude", "agent_status": "idle"},
                 {"pane_id": "w:p3", "tab_id": "w:t2", "workspace_id": "w", "agent": "codex", "agent_status": "working"},
                 {"pane_id": "w:p4", "tab_id": "w:t2", "workspace_id": "w", "agent": "codex", "agent_status": "idle"},
                 {"pane_id": "v:p5", "tab_id": "v:t1", "workspace_id": "v"},
                 {"pane_id": "v:p6", "tab_id": "v:t1", "workspace_id": "v", "agent": "kiro", "agent_status": "working"}]
        out = type("Out", (), {"stdout": panel.json.dumps({"result": {"panes": panes}})})
        with patch.object(panel.subprocess, "run", return_value=out), \
                patch.object(panel, "pane_agent", side_effect=lambda p: "OpenCode" if p == "v:p5" else None):
            found, working = panel.herdr_sessions("w:p1")
        self.assertEqual(found["tab"], {"Claude": {"panes": 1, "working": 0}})
        self.assertEqual(found["workspace"], {"Claude": {"panes": 1, "working": 0},
                                              "Codex": {"panes": 2, "working": 1}})
        self.assertEqual(found["all"], {"Claude": {"panes": 1, "working": 0}, "Codex": {"panes": 2, "working": 1},
                                        "OpenCode": {"panes": 1, "working": 0}, "Kiro": {"panes": 1, "working": 1}})
        self.assertEqual(working, {"Codex": {"w:p3"}, "Kiro": {"v:p6"}})

    def test_card_settings_list_under_the_usage_panel_heading(self):
        sections = [s.section for s in preferences.SETTINGS]
        usage = [i for i, s in enumerate(sections) if s == "Usage panel"]
        self.assertEqual(usage, list(range(usage[0], usage[-1] + 1)))
        keys = [preferences.SETTINGS[i].key for i in usage]
        self.assertEqual(keys[:2], [panel.THEME.key, panel.ANIMATE.key])
        self.assertIn(panel.CARD_KEY + "Codex", keys)
        self.assertEqual(preferences.get(panel.CARD_KEY + "Codex"), "all")


@unittest.skipIf(os.name == "nt", "POSIX input decoding; Windows reads keys through msvcrt")
class KeyTests(unittest.TestCase):
    def keys(self, data):
        import os
        from platform_runtime import Terminal
        read, write = os.pipe()   # the write end stays open, so a drained pipe isn't readable
        self.addCleanup(os.close, read)
        self.addCleanup(os.close, write)
        os.write(write, data)
        terminal = Terminal()
        terminal.fd = read
        keys = []
        while terminal.wait(0):
            keys.append(terminal.read_key())
        return keys, getattr(terminal, "releases", False)

    def test_kitty_keys_read_as_plain_keys(self):
        # What Herdr sends with the protocol on: Tab, T, ctrl+c, Enter, Esc, up, 2, q.
        keys, releases = self.keys(b"\x1b[9u\x1b[116;2:1u\x1b[99;5:1u\x1b[13u\x1b[27u"
                                   b"\x1b[1;1:1A\x1b[50;1:1u\x1b[113;1:1u\x1b[57441u")
        self.assertEqual(keys, ["\t", "T", "\x03", "\r", "esc", "up", "2", "q", ""])
        self.assertFalse(releases)

    def test_kitty_release_and_repeat(self):
        keys, releases = self.keys(b"\x1b[9u\x1b[9;1:2u\x1b[9;1:3u\x1b[1;1:3B")
        self.assertEqual(keys, ["\t", "\t", "release:\t", "release:down"])
        self.assertTrue(releases)

    def test_plain_keys_and_arrows_still_read(self):
        keys, _ = self.keys(b"\tq\x1b[A\x1bOB\x1b[1;2C\x1b[15~2")
        self.assertEqual(keys, ["\t", "q", "up", "down", "right", "", "2"])

    def test_unbound_sequences_are_ignored_instead_of_quitting(self):
        # Home, PgUp, Shift+Tab both ways, focus, SGR and X10 mouse, an overlong
        # sequence, Alt+q, and kitty codes outside ASCII (once crashes) then a real key.
        keys, _ = self.keys(b"\x1b[H\x1b[5~\x1b[Z\x1b[9;2u\x1b[I\x1b[<35;10;5M\x1b[M#qs"
                            b"\x1b[" + b"1" * 40 + b"u\x1b[113;3u\x1b[99999999u\x1b[304;5u\x1b[178uj")
        self.assertEqual(keys, [""] * 12 + ["j"])


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


class RateLimitTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.file = Path(folder.name) / "usage-holds.json"
        for patcher in (patch.object(panel, "HOLDS_FILE", self.file),
                        patch.object(panel.ap, "STATE", Path(folder.name)),
                        patch.object(panel, "merge_cache", lambda: None),
                        patch.object(panel, "record", lambda name, windows: None)):
            patcher.start()
            self.addCleanup(patcher.stop)
        for state in (panel.HOLDS, panel.FORCED, panel.LAST_TRY, panel.FAILS, panel.USAGE):
            saved = patch.dict(state, {}, clear=True)
            saved.start()
            self.addCleanup(saved.stop)

    def too_many(self, retry="44"):
        def fetcher():
            raise urllib.error.HTTPError("https://example", 429, "Too Many Requests",
                                         {"Retry-After": retry}, io.BytesIO(b""))
        return patch.dict(panel.ap.FETCHERS, {"Claude": fetcher})

    def test_429_holds_every_reader_for_retry_after(self):
        with self.too_many("44"):
            panel.fetch("Claude")
        now = time.time()
        self.assertAlmostEqual(panel.HOLDS["Claude"], now + 44, delta=2)
        self.assertAlmostEqual(json.loads(self.file.read_text())["Claude"], now + 44, delta=2)

    def test_refresh_waits_out_a_hold_another_process_wrote(self):
        now = time.time()
        self.file.write_text(json.dumps({"Claude": now + 40}))
        panel.force_all(["Claude"])
        with panel.LOCK:
            self.assertAlmostEqual(panel.next_read("Claude", now), now + 40, delta=1)
            self.assertLessEqual(panel.next_read("Codex", now), now)

    def test_a_fresh_read_from_another_process_counts_as_this_ones(self):
        now = time.time()
        panel.USAGE["Claude"] = {"windows": [{"label": "5h", "pct": 1, "resets": now + 3600}], "fetched": now - 5}
        with panel.LOCK:
            self.assertGreater(panel.next_read("Claude", now), now)

    def test_held_card_says_limited_instead_of_a_stale_age(self):
        now = time.time()
        panel.USAGE["Claude"] = {"windows": [{"label": "5h", "pct": 1, "resets": now + 3600}], "fetched": now - 2640}
        panel.HOLDS["Claude"] = now + 40
        present = patch.dict(panel.PRESENT, {"Claude": 1}, clear=True)
        present.start()
        self.addCleanup(present.stop)
        view = panel.View([a for a in panel.ap.AGENTS if a["name"] == "Claude"])
        text = re.sub(r"\x1b\[[0-9;]*m", "", "".join(panel.card(view, 0, 60, 0.0, now)))
        self.assertIn("limited 40s", text)
        self.assertNotIn("ago", text)


if __name__ == "__main__":
    unittest.main()
