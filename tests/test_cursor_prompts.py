"""Cursor's native prompt layer and Herdr's additive layer stay independent."""

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
import agent_picker
import cursor_prompts
import provider_defaults
import session_refresh
import settings
import star_engines
import system_prompts

FLAG = cursor_prompts.FLAG


class CursorPromptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.config = self.home / "config.json"
        home = patch.object(Path, "home", return_value=self.home)
        home.start()
        self.addCleanup(home.stop)
        environment = patch.dict(os.environ, HOME=str(self.home), AGENT_PICKER_CONFIG=str(self.config),
                                 AGENT_PICKER_NO_MODEL_REFRESH="1")
        environment.start()
        self.addCleanup(environment.stop)
        self.agent = next(a for a in settings.load_agents() if a["name"] == "Cursor")

    def test_only_providers_with_an_adapter_get_the_toggle(self):
        agents = {a["name"]: a for a in settings.load_agents()}
        for name, agent in agents.items():
            with self.subTest(name=name):
                fields = provider_defaults.Menu(agent).fields() if provider_defaults.current(agent) else ()
                self.assertEqual("system_prompts" in fields, name in system_prompts.ADAPTERS)
                if name not in system_prompts.ADAPTERS:
                    self.assertEqual(system_prompts.prepare(agent, ["/vendor/x", "--a"]), ["/vendor/x", "--a"])

    def test_menu_and_persistence_preserve_other_launch_defaults(self):
        before = list(self.agent["argv"])
        self.config.write_text(json.dumps({"keep": 1, "agents": {"Cursor": {"command": "/vendor/cursor-agent"}}}))
        menu = provider_defaults.Menu(self.agent)
        self.assertTrue(menu.values["system_prompts"])
        menu.index = menu.fields().index("system_prompts")
        menu.handle("left")
        self.assertEqual(menu.review()[1], [("System Prompts", "On", "Off")])
        provider_defaults.save(self.agent, menu.values, self.config)
        reloaded = next(a for a in settings.load_agents() if a["name"] == "Cursor")
        self.assertFalse(provider_defaults.current(reloaded)["system_prompts"])
        self.assertEqual(reloaded["argv"], ["/vendor/cursor-agent", *before[1:]])
        self.assertEqual(json.loads(self.config.read_text())["keep"], 1)
        rendered = re.sub(r"\x1b\[[0-9;]*m", "", "\n".join(
            agent_picker.render_defaults(provider_defaults.Menu(reloaded), 90, 28, 0)))
        self.assertIn("System Prompts", rendered)
        with self.assertRaisesRegex(ValueError, "System Prompts"):
            provider_defaults.apply(reloaded, {"system_prompts": "off"})

    def test_on_leaves_argv_alone_and_off_adds_the_flag_once(self):
        argv = ["/vendor/cursor-agent", "--model", "grok-4.7-high", "--yolo", "Herdr opening"]
        self.assertEqual(system_prompts.prepare(self.agent, argv), argv)
        provider_defaults.apply(self.agent, {"system_prompts": False})
        prepared = system_prompts.prepare(self.agent, argv)
        self.assertEqual(prepared, [argv[0], FLAG, *argv[1:]])
        self.assertEqual(system_prompts.prepare(self.agent, prepared), prepared)
        self.assertEqual(argv[1:], ["--model", "grok-4.7-high", "--yolo", "Herdr opening"])

    def test_star_headless_and_native_use_the_same_setting(self):
        provider_defaults.apply(self.agent, {"system_prompts": False})
        with patch.object(star_engines, "resolve_argv", return_value=["/vendor/cursor-agent"]):
            engine = star_engines.CursorEngine(self.agent)
            headless = engine.argv("task", "grok-4.7-high", "high", session="sid")
            self.assertEqual(headless[:2], ["/vendor/cursor-agent", FLAG])
            self.assertEqual(headless[-1], "task")
            self.assertEqual(engine.native("sid"), ["/vendor/cursor-agent", FLAG, "--resume", "sid"])

    def test_refresh_keeps_the_flag_and_drops_the_old_opening(self):
        argv = ["/vendor/cursor-agent", "--use-system-ca", "/vendor/index.js", FLAG, "--yolo", "Reply ready"]
        self.assertEqual(session_refresh.resume_argv("Cursor", argv, "sid"), argv[:-1] + ["--resume", "sid"])

    def test_fake_provider_receives_four_independent_layer_combinations(self):
        script = self.home / "fake-cursor.py"
        fake = self.home / ("fake-cursor.cmd" if os.name == "nt" else "fake-cursor")
        captured = self.home / "argv.json"
        body = ("import json,sys\nfrom pathlib import Path\n" +
                "Path(" + repr(str(captured)) + ").write_text(json.dumps(sys.argv[1:]), encoding='utf-8')\n")
        script.write_text(body, encoding="utf-8")
        if os.name == "nt":
            fake.write_text('@echo off\r\n"' + sys.executable + '" "' + str(script) + '" %*\r\n', encoding="utf-8")
        else:
            fake.write_text("#!" + sys.executable + "\n" + body, encoding="utf-8")
            fake.chmod(0o755)
        env = dict(os.environ, AGENT_PICKER_STATE_HOME=str(self.home / "state"),
                   AGENT_PICKER_NO_PRETRUST="1", HERDR_TAB_ID="test:tab", HERDR_ENV="0")
        plain = None
        for herdr_on in (False, True):
            for cursor_on in (True, False):
                with self.subTest(cursor=cursor_on, herdr=herdr_on):
                    self.config.write_text(json.dumps({"agents": {"Cursor": {
                        "command": str(fake), "defaults": {"system_prompts": cursor_on}}}}))
                    role = (["--picker-peer", "test:pane", "--picker-peers", "test:pane,test:other"]
                            if herdr_on else ["--picker-plain"])
                    result = subprocess.run([sys.executable, str(ROOT / "src/agent_picker.py"),
                                             "--picker-launch", "Cursor", *role], env=env,
                                            capture_output=True, text=True, encoding="utf-8", timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    argv = json.loads(captured.read_text())
                    self.assertEqual(FLAG in argv, not cursor_on)
                    self.assertIn("--yolo", argv)
                    self.assertTrue(any(a.startswith("grok-4.7") for a in argv), argv)
                    rest = [a for a in argv if a != FLAG]
                    if not herdr_on:
                        plain = plain or rest
                        self.assertEqual(rest, plain)   # the flag is the only difference
                    else:
                        self.assertGreater(len(rest), len(plain))   # Herdr's opening still arrives


if __name__ == "__main__":
    unittest.main()
