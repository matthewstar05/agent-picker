import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import agent_picker as picker
import provider_defaults as defaults
import settings
import team


class CodexFastModeTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.config = self.root / "config.json"
        env = patch.dict(os.environ, AGENT_PICKER_CONFIG=str(self.config),
                         AGENT_PICKER_STATE_HOME=str(self.root / "state"),
                         AGENT_PICKER_NO_MODEL_REFRESH="1", AGENT_PICKER_NO_PRETRUST="1")
        env.start()
        self.addCleanup(env.stop)

    def codex(self):
        return next(a for a in settings.load_agents() if a["name"] == "Codex")

    def test_toggle_is_codex_only_and_reviewed_before_saving(self):
        for agent in settings.load_agents():
            menu = defaults.Menu(agent)
            self.assertEqual("fast_mode" in menu.fields(), agent["name"] == "Codex")
        menu = defaults.Menu(self.codex())
        self.assertFalse(menu.values["fast_mode"])
        menu.index = menu.fields().index("fast_mode")
        for key in ("right", "left", " ", "\r"):
            before = menu.values["fast_mode"]
            menu.handle(key)
            self.assertIs(menu.values["fast_mode"], not before)
        menu.handle("right")
        self.assertTrue(menu.changed)
        self.assertEqual(menu.handle("esc"), "save")
        self.assertIn(("Fast Mode", "Off", "On"), menu.review()[1])
        self.assertFalse(self.config.exists())

    def test_saved_on_and_off_reach_fake_codex_and_preserve_other_defaults(self):
        script, output = self.root / "fake.py", self.root / "output.json"
        script.write_text("import json,sys\nfrom pathlib import Path\n"
                          "Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))\n")
        self.config.write_text(json.dumps({"keep": True, "agents": {"Codex": {
            "argv": [sys.executable, str(script), str(output), "-m", "chosen-model",
                     "-c", "model_reasoning_effort=xhigh", "-c", "keep=value",
                     "--dangerously-bypass-approvals-and-sandbox"], "keep": "local"}}}))
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                agent = self.codex()
                values = dict(defaults.current(agent), fast_mode=enabled)
                defaults.save(agent, values, self.config)
                reloaded = self.codex()
                self.assertIs(defaults.current(reloaded)["fast_mode"], enabled)
                self.assertEqual(" · fast" in reloaded["detail"], enabled)
                result = subprocess.run([sys.executable, str(ROOT / "src/agent_picker.py"),
                                         "--picker-launch", "Codex", "prompt with spaces"],
                                        capture_output=True, text=True, encoding="utf-8")
                self.assertEqual(result.returncode, 0, result.stderr)
                args = json.loads(output.read_text())
                self.assertIn("service_tier=" + ("fast" if enabled else "default"), args)
                self.assertIn("features.fast_mode=true", args)
                self.assertIn("chosen-model", args)
                self.assertIn("model_reasoning_effort=xhigh", args)
                self.assertIn("--dangerously-bypass-approvals-and-sandbox", args)
                self.assertIn("keep=value", args)
                self.assertEqual(args[-1], "prompt with spaces")
                data = json.loads(self.config.read_text())
                self.assertTrue(data["keep"])
                self.assertEqual(data["agents"]["Codex"]["keep"], "local")
                self.assertEqual(data["agents"]["Codex"]["argv"][0], sys.executable)

    def test_existing_config_override_forms_are_replaced_without_duplicates(self):
        agent = self.codex()
        agent["argv"] += ["--config=service_tier='priority'", "-cservice_tier=fast",
                          "--config", 'service_tier = "priority"',
                          "--config=features.fast_mode=false", "-ckeep=unchanged"]
        self.assertTrue(defaults.current(agent)["fast_mode"])
        defaults.apply(agent, {"fast_mode": False})
        configs = [(key, value) for _, _, key, value in defaults.codex_configs(agent["argv"])]
        self.assertEqual([value for key, value in configs if key == "service_tier"], ["default"])
        self.assertEqual([value for key, value in configs if key == "features.fast_mode"], ["true"])
        self.assertIn("-ckeep=unchanged", agent["argv"])

    def test_older_saved_defaults_inherit_standard_mode(self):
        self.config.write_text(json.dumps({"agents": {"Codex": {"defaults": {
            "model": "gpt-6.1-sol", "effort": "high", "bypass_permissions": True}}}}))
        agent = self.codex()
        self.assertFalse(defaults.current(agent)["fast_mode"])
        self.assertIn("service_tier=default", agent["argv"])
        self.assertIn("features.fast_mode=true", agent["argv"])

    def test_fast_mode_requires_boolean_and_failure_does_not_mutate_profile(self):
        for value in ("false", 1, None):
            agent = self.codex()
            before = copy.deepcopy(agent)
            with self.assertRaisesRegex(ValueError, "Fast Mode"):
                defaults.apply(agent, {"fast_mode": value})
            self.assertEqual(agent, before)

    def test_team_model_overrides_keep_fast_mode(self):
        agent = self.codex()
        defaults.apply(agent, {"fast_mode": True})
        for role in ("--picker-lead", "--picker-crew"):
            prepared, _ = team.prepare(agent, [role, "pane-1"])
            self.assertTrue(defaults.current(prepared)["fast_mode"])
            self.assertIn("service_tier=fast", prepared["argv"])

    def test_fast_mode_row_and_quota_hint_fit_resized_terminals(self):
        menu = defaults.Menu(self.codex())
        menu.index = menu.fields().index("fast_mode")
        for cols in (1, 8, 24, 40, 80, 120):
            for rows in (1, 4, 7, 12, 24):
                lines = picker.render_defaults(menu, cols, rows, 0)
                self.assertLessEqual(len(lines), rows)
                self.assertTrue(all(picker.visible_len(line) <= cols for line in lines), (cols, rows))
        visible = "\n".join(picker._ANSI.sub("", line)
                            for line in picker.render_defaults(menu, 80, 24, 0))
        self.assertIn("Fast Mode", visible)
        self.assertIn("◂ Off ▸", visible)
        self.assertIn("Faster replies use more quota.", visible)


if __name__ == "__main__":
    unittest.main()
