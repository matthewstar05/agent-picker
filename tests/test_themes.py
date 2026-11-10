import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import preferences
import themes
import usage_panel as panel

BUILTIN_NAMES = ["brand", "aurora", "sunset", "vapor", "ocean", "matrix", "ember",
                 "sakura", "glacier", "mono", "rainbow", "latte", "solar", "galaxy", "rose"]


class ThemeTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = Path(folder.name)
        self.local = self.dir / "config" / "themes.json"
        self.local.parent.mkdir()
        self.clock = [1000.0]
        self.writes = 0
        for patcher in (patch.object(themes.settings, "config_path", lambda: self.local.parent / "config.json"),
                        patch.object(themes, "time", SimpleNamespace(monotonic=lambda: self.clock[0])),
                        patch.object(themes, "_files", {}),
                        patch.object(themes, "_checked", None),
                        patch.object(themes, "_tracked", list(themes._tracked)),
                        patch.object(preferences, "PATH", self.dir / "preferences.json"),
                        patch.object(preferences, "_values", {})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.saved = list(themes.THEMES), list(panel.THEME.choices)
        self.addCleanup(self.restore)
        themes.reload(force=True)

    def restore(self):
        themes.THEMES[:] = self.saved[0]
        panel.THEME.choices[:] = self.saved[1]

    def write(self, data):
        text = data if isinstance(data, str) else json.dumps(data)
        self.local.write_text(text, encoding="utf-8")
        # a distinct stamp even on filesystems with coarse mtimes
        self.writes += 1
        stamp = 1_700_000_000 + self.writes
        os.utime(self.local, (stamp, stamp))

    def later(self, seconds=themes.RECHECK):
        self.clock[0] += seconds

    def test_builtins_load_in_order_from_the_checkout_file(self):
        self.assertEqual(themes.names(), BUILTIN_NAMES)
        self.assertIsNone(themes.get("brand")["stops"])
        self.assertEqual(themes.stops("rainbow"), "rainbow")
        self.assertEqual(themes.stops("aurora"), [(40, 230, 170), (60, 170, 255), (170, 110, 255)])
        self.assertEqual(themes.problems(), [])
        self.assertEqual(themes.sources(), [(themes.BUILTIN, True), (self.local, False)])
        self.assertEqual(json.loads((ROOT / "themes.json").read_text())[0], {"name": "brand", "stops": None})

    def test_unknown_names_resolve_to_the_first_theme(self):
        self.assertEqual(themes.index("no-such-theme"), 0)
        self.assertIs(themes.get("no-such-theme"), themes.THEMES[0])
        self.assertEqual(themes.index("mono"), BUILTIN_NAMES.index("mono"))

    def test_hex_colors_and_the_themes_wrapper_are_accepted(self):
        self.write({"themes": [{"name": "dusk", "stops": ["#ff8000", " #0A0b0C ", [1, 2, 3]]}]})
        self.later()
        self.assertTrue(themes.reload())
        self.assertEqual(themes.stops("dusk"), [(255, 128, 0), (10, 11, 12), (1, 2, 3)])
        self.assertEqual(themes.sources()[1], (self.local, True))

    def test_a_local_theme_replaces_a_builtin_in_place_and_new_ones_append_in_order(self):
        self.write([{"name": "zeta", "stops": ["#111111"]},
                    {"name": "ocean", "stops": ["#000080", "#00ffff"]},
                    {"name": "alpha", "stops": None}])
        self.later()
        themes.reload()
        self.assertEqual(themes.names(), BUILTIN_NAMES + ["zeta", "alpha"])
        self.assertEqual(themes.stops("ocean"), [(0, 0, 128), (0, 255, 255)])
        self.assertEqual(themes.index("ocean"), BUILTIN_NAMES.index("ocean"))

    def test_invalid_entries_are_skipped_with_problems(self):
        self.write([{"name": "good", "stops": ["#123456"]},
                    "not a theme",
                    {"stops": ["#123456"]},
                    {"name": "nostops"},
                    {"name": "empty", "stops": []},
                    {"name": "many", "stops": ["#000000"] * 9},
                    {"name": "short", "stops": [[1, 2]]},
                    {"name": "bright", "stops": [[1, 2, 300]]},
                    {"name": "bool", "stops": [[True, 2, 3]]},
                    {"name": "badhex", "stops": ["#12345"]},
                    {"name": "brandish", "stops": "brand"},
                    {"name": "eight", "stops": [[k, k, k] for k in range(8)]}])
        self.later()
        themes.reload()
        self.assertEqual(themes.names(), BUILTIN_NAMES + ["good", "eight"])
        problems = themes.problems()
        self.assertEqual(len(problems), 10)
        self.assertTrue(all(str(self.local) in p and "skipped" in p for p in problems), problems)
        self.assertIn("theme 2 skipped", problems[0])

    def test_a_malformed_file_keeps_the_last_good_themes(self):
        self.write("[{not json")
        self.later()
        self.assertFalse(themes.reload())
        self.assertEqual(themes.names(), BUILTIN_NAMES)     # built-ins on the first load
        self.assertEqual(len(themes.problems()), 1)
        self.write([{"name": "kept", "stops": ["#abcdef"]}])
        self.later()
        self.assertTrue(themes.reload())
        self.assertEqual(themes.problems(), [])
        for broken in ('{"themes": 3}', "{oops", '"just a string"'):
            self.write(broken)
            self.later()
            self.assertFalse(themes.reload(), broken)
            self.assertEqual(themes.names(), BUILTIN_NAMES + ["kept"], broken)
            self.assertIn("keeping the themes it had", themes.problems()[0])
        self.local.unlink()
        self.later()
        self.assertTrue(themes.reload())    # a missing file is fine: just the built-ins
        self.assertEqual(themes.names(), BUILTIN_NAMES)
        self.assertEqual(themes.problems(), [])

    def test_reload_is_rate_limited_and_only_rereads_changed_files(self):
        self.write([{"name": "one", "stops": ["#010101"]}])
        self.later(0.5)
        self.assertFalse(themes.reload())          # too soon to look
        self.assertNotIn("one", themes.names())
        self.assertTrue(themes.reload(force=True))
        self.assertIn("one", themes.names())
        with patch.object(themes, "_read", side_effect=AssertionError("unchanged files are not reread")):
            self.later()
            self.assertFalse(themes.reload())
            self.assertFalse(themes.reload(force=True))
        self.later()
        self.write([{"name": "two", "stops": ["#020202"]}])
        self.assertTrue(themes.reload())
        self.assertEqual(themes.names()[-1], "two")
        before = list(themes.THEMES)
        self.later()
        self.write([{"name": "two", "stops": ["#020202"]}])     # same themes, new stamp
        self.assertFalse(themes.reload())
        self.assertEqual(themes.THEMES, before)

    def test_reload_changes_the_shared_list_in_place(self):
        shared = themes.THEMES
        self.write([{"name": "inplace", "stops": "rainbow"}])
        self.later()
        themes.reload()
        self.assertIs(themes.THEMES, shared)
        self.assertIs(panel.THEMES, themes.THEMES)
        self.assertEqual(shared[-1], {"name": "inplace", "stops": "rainbow"})

    def test_track_keeps_choices_in_sync_with_extra_choices_first(self):
        setting = preferences.Setting("test.fs.theme", "Test", "Theme", ["stale"], "usage")
        choices = setting.choices
        self.assertIs(themes.track(setting, extra=["usage"]), setting)
        self.assertEqual(setting.choices, ["usage"] + BUILTIN_NAMES)
        self.write([{"name": "new", "stops": ["#ffffff"]}])
        self.later()
        themes.reload()
        self.assertIs(setting.choices, choices)
        self.assertEqual(setting.choices, ["usage"] + BUILTIN_NAMES + ["new"])
        self.assertEqual(panel.THEME.choices, BUILTIN_NAMES + ["new"])
        themes.track(setting, extra=["usage", "agent"])      # tracking again replaces the extra choices
        self.local.unlink()
        self.later()
        themes.reload()
        self.assertEqual(setting.choices, ["usage", "agent"] + BUILTIN_NAMES)

    def test_a_new_local_theme_can_be_cycled_to_in_the_usage_panel(self):
        present = patch.dict(panel.PRESENT, {a["name"]: 1 for a in panel.ap.AGENTS}, clear=True)
        present.start()
        self.addCleanup(present.stop)
        self.write([{"name": "custom", "stops": ["#ff0000", "#0000ff"]}])
        self.later()
        view = panel.View([a for a in panel.ap.AGENTS if a["name"] in panel.ap.FETCHERS])
        preferences.put(panel.THEME.key, BUILTIN_NAMES[-1])
        self.assertEqual(view.theme, len(BUILTIN_NAMES) - 1)    # the view rereads the files
        panel.handle(view, "t", {})
        self.assertEqual(preferences.get(panel.THEME.key), "custom")
        self.assertEqual(view.theme, len(BUILTIN_NAMES))
        self.assertEqual(view.frame_style(), {"gradient": [(255, 0, 0), (0, 0, 255)]})
        lines = panel.render(view, 80, 20, 1.5)
        self.assertLessEqual(len(lines), 20)
        panel.handle(view, "t", {})
        self.assertEqual(preferences.get(panel.THEME.key), "brand")
        panel.handle(view, "T", {})
        self.assertEqual(preferences.get(panel.THEME.key), "custom")
        self.local.unlink()
        self.later()
        self.assertEqual(view.theme, themes.index(panel.THEME.default))     # gone again: the default
        self.assertEqual(preferences.get(panel.THEME.key), panel.THEME.default)

    def test_a_missing_builtin_file_never_leaves_the_list_empty(self):
        with patch.object(themes, "BUILTIN", self.dir / "missing.json"):
            themes._files.clear()
            themes.reload(force=True)
            self.assertEqual(themes.names(), ["brand"])
            self.assertIn("missing", themes.problems()[0])


if __name__ == "__main__":
    unittest.main()
