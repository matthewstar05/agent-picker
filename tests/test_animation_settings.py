"""Regression coverage for the Usage panel's Animation setting."""

from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import usage_panel
import preferences
import scheduler_menu


class AnimationSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "preferences.json"
        for name, value in (("PATH", self.path), ("_values", None), ("_stamp", None),
                            ("_checked", 0)):
            patcher = patch.object(preferences, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_key_on_usage_and_scheduler_lists_does_not_toggle_animation(self):
        with patch.object(scheduler_menu.scheduler, "service_status", return_value=True), \
             patch.object(scheduler_menu.scheduler, "profiles", return_value={}), \
             patch.object(scheduler_menu.scheduler, "read", return_value={"jobs": []}):
            menu = scheduler_menu.Menu(cwd=self.temp.name)

        view = usage_panel.View([], pinned=True)
        for initial in (True, False):
            with self.subTest(initial=initial):
                preferences.put(usage_panel.ANIMATE.key, initial)
                with patch.object(preferences, "put", wraps=preferences.put) as put:
                    usage_panel.handle(view, "a", [])
                    menu.handle("a")
                self.assertFalse(put.called)
                preferences._values = None
                self.assertIs(preferences.get(usage_panel.ANIMATE.key), initial)

    def test_animation_is_available_and_persists_from_settings_menu(self):
        setting = preferences.find(usage_panel.ANIMATE.key)
        self.assertIsNotNone(setting)
        self.assertEqual((setting.section, setting.label), ("Usage panel", "Animation"))
        menu = preferences.Menu()
        menu.index = menu.items.index(setting)

        self.assertIs(preferences.get(setting.key), setting.default)
        menu.handle("right")
        self.assertIs(preferences.get(setting.key), not setting.default)

        preferences._values = None
        self.assertIs(preferences.get(setting.key), not setting.default)
        self.assertIn(setting.key, json.loads(self.path.read_text()))


if __name__ == "__main__":
    unittest.main()
