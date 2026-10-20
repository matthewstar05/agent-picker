import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import cursor_pools as pools
import preferences
import usage_panel as panel


def bar_colors(line):
    color, result = None, []
    for token in re.findall(r"\x1b\[[0-9;]*m|[^\x1b]", line):
        if token == panel.RESET:
            color = None
        elif token.startswith("\x1b[38;2;"):
            color = token
        elif token in "▰▱":
            result.append((token, color))
    return result


class CursorPoolTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for context in (patch.object(preferences, "PATH", Path(folder.name) / "prefs.json"),
                        patch.object(preferences, "_values", {}),
                        patch.dict(panel.CURSOR_POOLS, {}, clear=True),
                        patch.dict(panel._pool_cache, {}, clear=True),
                        patch.dict(panel.STAR_POOLS, {}, clear=True)):
            context.start()
            self.addCleanup(context.stop)

    def test_models_and_live_footer_follow_the_selected_allowance(self):
        for model in ("auto", "grok-4.7-high", "grok-4.6", "composer-2.5"):
            self.assertEqual(pools.model_pool(model), "cr", model)
        for model in ("claude-opus-5-5-high", "gpt-5.4", "gemini-3.5-pro"):
            self.assertEqual(pools.model_pool(model), "ex", model)
        self.assertEqual(pools.argv_pool(["agent", "--model=claude-opus-5-5-high"]), "ex")
        self.assertEqual(pools.screen_pool("Grok 4.7 High                Run Everything\n~"), "cr")
        self.assertEqual(pools.screen_pool("Claude Opus 5.5 · 2 files    Run Everything\n300K Medium"), "ex")
        self.assertIsNone(pools.screen_pool("Grok 4.7 is mentioned in a user request"))
        self.assertIsNone(pools.model_pool(None))

    def test_live_model_change_overrides_original_process_argument_and_is_cached(self):
        pane = {"pane_id": "w:p", "agent": "cursor", "agent_session": {"value": "s"}}
        with patch.object(panel.subprocess, "run", return_value=SimpleNamespace(stdout="Grok 4.7 High     Run Everything")) as run:
            self.assertEqual(panel.cursor_pool(pane), "cr")
            self.assertEqual(panel.cursor_pool(pane), "cr")
            self.assertEqual(run.call_count, 1)
        panel._pool_cache.clear()
        out = SimpleNamespace(stdout=json.dumps({"result": {"process_info": {"foreground_processes": [
            {"argv": ["cursor-agent", "--model", "claude-opus-5-5-high"]}]}}}))
        with patch.object(panel.subprocess, "run", side_effect=[SimpleNamespace(stdout="no footer"), out]):
            self.assertEqual(panel.cursor_pool(pane), "ex")
        with patch.object(panel.subprocess, "run", side_effect=AssertionError("Grok Bot needs no footer read")):
            self.assertEqual(panel.cursor_pool({"pane_id": "w:g", "agent": "grok"}), "gb")

    def test_each_scope_counts_only_the_working_panes_for_its_pool(self):
        panes = [{"pane_id": "w:me", "tab_id": "w:t", "workspace_id": "w"},
                 {"pane_id": "w:g", "tab_id": "w:t", "workspace_id": "w", "agent": "grok", "agent_status": "working"},
                 {"pane_id": "w:cr", "tab_id": "w:other", "workspace_id": "w", "agent": "cursor", "agent_status": "working"},
                 {"pane_id": "v:ex", "tab_id": "v:t", "workspace_id": "v", "agent": "cursor", "agent_status": "working"},
                 {"pane_id": "w:idle", "tab_id": "w:t", "workspace_id": "w", "agent": "cursor", "agent_status": "idle"}]
        out = SimpleNamespace(stdout=json.dumps({"result": {"panes": panes}}))
        with patch.object(panel.subprocess, "run", return_value=out), patch.object(panel, "star_turns", return_value=[]), \
             patch.object(panel, "cursor_pool", side_effect=lambda p: {"w:g": "gb", "w:cr": "cr", "v:ex": "ex"}[p["pane_id"]]):
            found, working = panel.herdr_sessions("w:me")
        self.assertEqual(found["tab"]["Cursor"], {"panes": 2, "working": 1, "pools": {"gb": 1}})
        self.assertEqual(found["workspace"]["Cursor"]["pools"], {"gb": 1, "cr": 1})
        self.assertEqual(found["all"]["Cursor"]["pools"], {"gb": 1, "cr": 1, "ex": 1})
        self.assertEqual(len(working["Cursor"]), 3)

    def test_only_active_bar_colors_move_while_entire_background_keeps_moving(self):
        now = 1800000000
        agent = next(a for a in panel.ap.AGENTS if a["name"] == "Cursor")
        entry = {"fetched": now, "windows": [{"label": label, "pct": 30, "resets": now + 86400}
                                            for label in ("cr", "ex", "gb")]}
        view = panel.View([agent], pinned=True)
        for theme in panel.THEMES:
            preferences.put(panel.THEME.key, theme["name"])
            for active in ("cr", "ex", "gb", None):
                panel.CURSOR_POOLS.clear()
                if active:
                    panel.CURSOR_POOLS[active] = 1
                before = panel._card(view, 0, 66, 1, now, entry, False, 1, rows=4)
                after = panel._card(view, 0, 66, 2, now, entry, False, 1, rows=4)
                for index, label in enumerate(("cr", "ex", "gb"), 1):
                    colors = bar_colors(before[index]), bar_colors(after[index])
                    self.assertTrue(colors[0])
                    self.assertEqual(colors[0] == colors[1], label != active, (theme["name"], active, label))
                    self.assertNotEqual(re.findall(r"\x1b\[48;2;[^m]*m", before[index]),
                                        re.findall(r"\x1b\[48;2;[^m]*m", after[index]))
                with patch.dict(panel.USAGE, {"Cursor": entry}, clear=True), patch.dict(panel.WORKING, {"Cursor": 1}, clear=True):
                    compact = [panel.compact_row(view, 0, 120, t, now) for t in (1, 2)]
                    for index, label in enumerate(("cr", "ex", "gb")):
                        # At this width each compact pool has a full 16-cell bar.
                        colors = [bar_colors(line)[index * 16:(index + 1) * 16] for line in compact]
                        self.assertEqual(len(colors[0]), 16)
                        self.assertEqual(colors[0] == colors[1], label != active, (theme["name"], active, label, "compact"))


if __name__ == "__main__":
    unittest.main()
