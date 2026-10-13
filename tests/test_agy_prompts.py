"""The native agy prompt layer and Herdr's additive layer stay independent."""

import copy
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import agy_prompts
import agent_picker
import provider_defaults
import settings
import star_engines


class AgyPromptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.config = self.home / "config.json"
        environment = patch.dict(os.environ, HOME=str(self.home), AGENT_PICKER_CONFIG=str(self.config),
                                 AGENT_PICKER_NO_MODEL_REFRESH="1")
        environment.start()
        self.addCleanup(environment.stop)
        self.agent = next(a for a in settings.load_agents() if a["name"] == "Antigravity")

    def test_menu_and_persistence_preserve_other_launch_defaults(self):
        before = list(self.agent["argv"])
        self.config.write_text(json.dumps({"keep": 1, "agents": {"Antigravity": {"command": "/vendor/agy"}}}))
        menu = provider_defaults.Menu(self.agent)
        self.assertTrue(menu.values["system_prompts"])
        menu.index = menu.fields().index("system_prompts")
        menu.handle("left")
        self.assertEqual(menu.review()[1], [("System Prompts", "On", "Off")])
        provider_defaults.save(self.agent, menu.values, self.config)
        reloaded = next(a for a in settings.load_agents() if a["name"] == "Antigravity")
        self.assertFalse(provider_defaults.current(reloaded)["system_prompts"])
        self.assertEqual(reloaded["argv"], ["/vendor/agy", *before[1:]])
        self.assertEqual(json.loads(self.config.read_text())["keep"], 1)
        menu = provider_defaults.Menu(reloaded)
        menu.values["model"] = "other-model"
        self.assertIn("system_prompts", menu.fields())
        rendered = agent_picker.render_defaults(menu, 90, 28, 0)
        self.assertIn("System Prompts", re.sub(r"\x1b\[[0-9;]*m", "", "\n".join(rendered)))
        with self.assertRaisesRegex(ValueError, "System Prompts"):
            provider_defaults.apply(reloaded, {"system_prompts": "off"})

    def test_on_does_not_install_or_change_custom_agent(self):
        argv = ["/vendor/agy", "--agent", "mine", "--model", "unchanged"]
        self.assertEqual(agy_prompts.prepare(self.agent, argv), argv)
        self.assertFalse((self.home / ".gemini").exists())

    def test_off_registers_once_and_preserves_herdr_opening(self):
        provider_defaults.apply(self.agent, {"system_prompts": False})
        argv = self.agent["argv"] + ["-i", "Herdr standing instructions"]
        original = copy.deepcopy(self.agent)
        prepared = agy_prompts.prepare(self.agent, argv)
        self.assertEqual(prepared[3:], argv[1:])
        self.assertEqual(prepared[1:3], ["--agent", agy_prompts.NAME])
        self.assertEqual(agy_prompts.prepare(self.agent, prepared), prepared)
        self.assertEqual(self.agent, original)
        profile = self.home / ".gemini/config/agents" / (agy_prompts.NAME + ".md")
        self.assertEqual(profile.read_text(), agy_prompts.PROFILE)
        profile.write_text("user-owned file")
        with self.assertRaisesRegex(RuntimeError, "conflicts"):
            agy_prompts.prepare(self.agent, argv)
        self.assertEqual(profile.read_text(), "user-owned file")

    def test_off_rejects_explicit_custom_agent(self):
        provider_defaults.apply(self.agent, {"system_prompts": False})
        for option in (["--agent", "mine"], ["--agent=mine"]):
            with self.assertRaisesRegex(ValueError, "cannot be combined"):
                agy_prompts.prepare(self.agent, self.agent["argv"] + option)
        self.assertFalse((self.home / ".gemini").exists())

    def test_star_headless_and_native_use_the_same_setting(self):
        provider_defaults.apply(self.agent, {"system_prompts": False})
        with patch.object(star_engines, "resolve_argv", return_value=["/vendor/agy"]):
            engine = star_engines.AntigravityEngine(self.agent)
            for argv in (engine.argv("task", "gemini-3.8-flash-low", "low"), engine.native(None)):
                self.assertEqual(argv[1:3], ["--agent", agy_prompts.NAME])

    def test_fake_provider_receives_four_independent_layer_combinations(self):
        fake = self.home / "fake-agy"
        captured = self.home / "argv.json"
        fake.write_text("#!" + sys.executable + "\nimport json,sys\nfrom pathlib import Path\n" +
                        "Path(" + repr(str(captured)) + ").write_text(json.dumps(sys.argv[1:]))\n")
        fake.chmod(0o755)
        env = dict(os.environ, AGENT_PICKER_STATE_HOME=str(self.home / "state"),
                   AGENT_PICKER_NO_PRETRUST="1", HERDR_TAB_ID="test:tab")
        for agy_on in (True, False):
            for herdr_on in (True, False):
                with self.subTest(agy=agy_on, herdr=herdr_on):
                    self.config.write_text(json.dumps({"agents": {"Antigravity": {
                        "command": str(fake), "defaults": {"system_prompts": agy_on}}}}))
                    role = (["--picker-peer", "test:pane", "--picker-peers", "test:pane,test:other"]
                            if herdr_on else ["--picker-plain"])
                    result = subprocess.run([sys.executable, str(ROOT / "src/agent_picker.py"),
                                             "--picker-launch", "Antigravity", *role], env=env,
                                            capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    argv = json.loads(captured.read_text())
                    self.assertEqual("--agent" in argv, not agy_on)
                    self.assertEqual("-i" in argv, herdr_on)
                    self.assertIn("gemini-3.8-flash-high", argv)
                    self.assertIn("--dangerously-skip-permissions", argv)


if __name__ == "__main__":
    unittest.main()
