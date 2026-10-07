import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import agent_picker as picker
import model_catalog as catalog
import provider_defaults as defaults
import settings
import platform_runtime


class CatalogTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        change = patch.dict(os.environ, AGENT_PICKER_NO_MODEL_REFRESH="1", AGENT_PICKER_CONFIG=str(Path(folder.name) / "config.json"))
        change.start()
        self.addCleanup(change.stop)
        change = patch.object(catalog, "PATH", Path(folder.name) / "models.json")
        change.start()
        self.addCleanup(change.stop)

    def agent(self, name):
        return next(a for a in settings.load_agents() if a["name"] == name)

    def test_bundled_models_exist_for_installed_profiles(self):
        data = json.loads((ROOT / "models.json").read_text())
        self.assertEqual(set(data), set(defaults.PROVIDERS))
        for name, rows in data.items():
            self.assertTrue(rows, name)
            self.assertEqual(len({row["id"] for row in rows}), len(rows), name)

    def test_cursor_variants_group_and_preserve_fast(self):
        rows = catalog.cursor_models("Available models\ngrok-4.7-low-fast - Grok Low Fast\ngrok-4.7-high-fast - Grok High Fast\nauto - Auto (default)\nTip: example\n")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["id"], "grok-4.7-fast")
        self.assertEqual(rows[0]["efforts"], ["low", "high"])
        self.assertEqual(rows[0]["variants"]["high"], "grok-4.7-high-fast")
        self.assertEqual(rows[1]["variants"]["provider default"], "auto")

    def test_current_custom_model_remains_selectable(self):
        agent = {"name": "Cursor"}
        with tempfile.TemporaryDirectory() as folder, patch.object(catalog, "PATH", Path(folder) / "none"), patch.object(catalog, "_live", {}):
            rows = catalog.options(agent, "my-custom-model")
        self.assertEqual(rows[0]["id"], "my-custom-model")

    def test_model_selection_adjusts_unsupported_effort_and_launches_auto(self):
        agent = self.agent("Cursor")
        with patch.dict(os.environ, AGENT_PICKER_NO_MODEL_REFRESH="1"):
            menu = defaults.Menu(agent)
        auto = next(row for row in menu.models() if row["id"] == "auto")
        menu.select_model(auto)
        self.assertEqual(menu.values["effort"], "provider default")
        defaults.apply(agent, menu.values)
        self.assertEqual(defaults.option(agent["argv"], ("--model",)), "auto")

    def test_antigravity_effort_uses_matching_model_variant(self):
        agent = self.agent("Antigravity")
        values = dict(defaults.current(agent), effort="low")
        defaults.apply(agent, values)
        self.assertEqual(defaults.option(agent["argv"], ("--model",)), "gemini-3.8-flash-low")
        self.assertEqual(defaults.option(agent["argv"], ("--effort",)), "low")

    def test_catalog_efforts_and_model_browser_fit_small_panes(self):
        agent = self.agent("Codex")
        with patch.dict(os.environ, AGENT_PICKER_NO_MODEL_REFRESH="1"):
            menu = defaults.Menu(agent)
        menu.handle("\r")
        self.assertTrue(menu.browsing)
        for cols in (1, 8, 24, 40, 80, 120):
            for rows in (1, 4, 7, 12, 24):
                frame = picker.render_defaults(menu, cols, rows, 0)
                self.assertLessEqual(len(frame), rows)
                self.assertTrue(all(picker.visible_len(line) <= cols for line in frame), (cols, rows))
        self.assertIn("ultra", menu.efforts())

    @unittest.skipIf(os.name == "nt", "POSIX input decoding")
    def test_escape_preserves_a_quick_following_key(self):
        terminal = platform_runtime.Terminal()
        terminal.fd = 0
        with patch.object(platform_runtime.os, "read", side_effect=[b"\x1b", b"y"]), patch.object(terminal, "wait", return_value=True):
            self.assertEqual(terminal.read_key(), "esc")
            self.assertEqual(terminal.read_key(), "y")


if __name__ == "__main__":
    unittest.main()
