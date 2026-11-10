"""Claude's Ultracode default: menu row, local persistence and the launch argv."""

import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import agent_picker
import claude_ultracode as ultracode
import model_catalog
import provider_defaults
import session_refresh
import settings
import team


class UltracodeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.config = self.home / "config.json"
        self.state = self.home / "state"
        environment = patch.dict(os.environ, AGENT_PICKER_CONFIG=str(self.config),
                                 AGENT_PICKER_STATE_HOME=str(self.state), AGENT_PICKER_NO_MODEL_REFRESH="1")
        environment.start()
        self.addCleanup(environment.stop)
        rows = patch.object(model_catalog, "load_rows", return_value=[])
        rows.start()
        self.addCleanup(rows.stop)
        self.agents = {a["name"]: a for a in settings.load_agents()}

    def claude(self, on=True):
        agent = copy.deepcopy(self.agents["Claude"])
        agent["argv"] = ["/vendor/claude", *agent["argv"][1:]]
        if on:
            provider_defaults.apply(agent, {"ultracode": True})
        return agent

    @staticmethod
    def flag(argv, name):
        return [argv[i + 1] for i, arg in enumerate(argv) if arg == name]

    def settings_of(self, argv):
        files = self.flag(argv, "--settings")
        self.assertEqual(len(files), 1, argv)
        self.assertTrue(Path(files[0]).resolve().is_relative_to(self.state.resolve()), files[0])
        return json.loads(Path(files[0]).read_text(encoding="utf-8"))

    def test_menu_toggle_and_save_round_trip(self):
        agent = self.agents["Claude"]
        self.config.write_text(json.dumps({"keep": 1, "agents": {"Claude": {"command": "/vendor/claude"}}}))
        menu = provider_defaults.Menu(agent)
        self.assertEqual(menu.fields(), ("model", "effort", "bypass_permissions", "ultracode"))
        self.assertIs(menu.values["ultracode"], False)
        menu.index = menu.fields().index("ultracode")
        rendered = re.sub(r"\x1b\[[0-9;]*m", "", "\n".join(agent_picker.render_defaults(menu, 90, 28, 0)))
        self.assertIn("Ultracode", rendered)
        self.assertIn("a workflow for every substantive", rendered)
        menu.handle("right")
        self.assertEqual(menu.review()[1], [("Ultracode", "Off", "On")])
        before = list(agent["argv"])
        provider_defaults.save(agent, menu.values, self.config)
        data = json.loads(self.config.read_text())
        self.assertEqual(data["keep"], 1)
        self.assertIs(data["agents"]["Claude"]["defaults"]["ultracode"], True)
        reloaded = next(a for a in settings.load_agents() if a["name"] == "Claude")
        self.assertIs(provider_defaults.current(reloaded)["ultracode"], True)
        self.assertTrue(reloaded["detail"].endswith(" · ultracode"))
        self.assertEqual(reloaded["argv"], ["/vendor/claude", *before[1:]])
        self.assertNotIn("--settings", reloaded["argv"])
        with self.assertRaisesRegex(ValueError, "Ultracode"):
            provider_defaults.apply(reloaded, {"ultracode": "on"})
        self.assertNotIn("ultracode", provider_defaults.Menu(self.agents["Codex"]).fields())

    def test_launch_adds_one_settings_file_and_guidance(self):
        agent = self.claude()
        argv = ultracode.prepare(agent, agent["argv"] + ["hello"])
        self.assertEqual(self.settings_of(argv), {"ultracode": True, "enableWorkflows": True})
        self.assertEqual(self.flag(argv, "--append-system-prompt"), [ultracode.GUIDANCE])
        self.assertEqual(argv[:len(agent["argv"])], agent["argv"])
        self.assertIn("hello", argv)

    def test_team_settings_and_brief_are_merged_once(self):
        agent = self.claude()
        with patch.object(team.handoff, "enabled", return_value=True), \
             patch.object(team.tempfile, "gettempdir", return_value=str(self.home)):
            prepared, args = team.prepare(agent, ["--picker-lead", "w:p2,w:p3"])
        brief = self.flag(prepared["argv"], "--append-system-prompt")[0]
        argv = ultracode.prepare(prepared, prepared["argv"] + args)
        self.assertEqual(self.settings_of(argv), {"deniedMcpServers": [{"serverName": "iMCP"}],
                                                  "ultracode": True, "enableWorkflows": True})
        self.assertEqual(self.flag(argv, "--append-system-prompt"), [brief + "\n\n" + ultracode.GUIDANCE])
        self.assertEqual(ultracode.prepare(prepared, argv), argv)

    def test_crew_panes_stay_without_ultracode(self):
        # Their brief says not to start subagents; a workflow per task would contradict it.
        agent = self.claude()
        with patch.object(team.handoff, "enabled", return_value=True), \
             patch.object(team.tempfile, "gettempdir", return_value=str(self.home)):
            prepared, args = team.prepare(agent, ["--picker-crew", "w:p1"])
        argv = prepared["argv"] + args
        self.assertEqual(ultracode.prepare(prepared, argv), argv)
        self.assertNotIn("ultracode", json.dumps([json.loads(Path(f).read_text()) for f in self.flag(argv, "--settings")]))

    def test_unreadable_settings_stop_the_launch(self):
        agent = self.claude()
        for value in (str(self.home / "missing.json"), "[1]"):
            with self.assertRaisesRegex(ValueError, "Ultracode"):
                ultracode.prepare(agent, ["/vendor/claude", "--settings", value])

    def test_a_windows_cmd_shim_gets_one_line_of_system_prompt(self):
        agent = self.claude()
        with patch.object(ultracode, "WINDOWS", True):
            argv = ultracode.prepare(agent, ["C:/npm/claude.cmd", "--append-system-prompt", "Brief"])
        self.assertEqual(self.flag(argv, "--append-system-prompt"), ["Brief " + ultracode.GUIDANCE])

    def test_inline_and_equals_settings_are_merged(self):
        agent = self.claude()
        argv = ultracode.prepare(agent, ["/vendor/claude", "--settings={\"model\": \"x\"}",
                                         "--append-system-prompt=Brief", "--", "prompt"])
        self.assertEqual(self.settings_of(argv), {"model": "x", "ultracode": True, "enableWorkflows": True})
        self.assertEqual(self.flag(argv, "--append-system-prompt"), ["Brief\n\n" + ultracode.GUIDANCE])
        self.assertEqual(argv[-2:], ["--", "prompt"])

    def test_idempotent(self):
        agent = self.claude()
        once = ultracode.prepare(agent, agent["argv"])
        self.assertEqual(ultracode.prepare(agent, once), once)
        self.assertEqual(len(list((self.state / "claude-settings").iterdir())), 1)

    def test_off_and_other_providers_are_untouched(self):
        agent = self.claude(on=False)
        self.assertEqual(ultracode.prepare(agent, agent["argv"]), agent["argv"])
        for name in ("Codex", "Antigravity", "Cursor", "Kiro"):
            other = copy.deepcopy(self.agents[name])
            other["defaults"] = dict(other.get("defaults") or {}, ultracode=True)
            self.assertEqual(ultracode.prepare(other, other["argv"]), other["argv"], name)
        self.assertFalse(self.state.exists())

    def test_session_refresh_keeps_both_flags(self):
        agent = self.claude()
        argv = ultracode.prepare(agent, agent["argv"] + ["hello"])
        resumed = session_refresh.resume_argv("Claude", argv, "session-id")
        self.assertEqual(resumed, [a for a in argv if a != "hello"] + ["--resume", "session-id"])
        self.assertEqual(len(self.flag(resumed, "--settings")), 1)
        self.assertEqual(self.flag(resumed, "--append-system-prompt"), [ultracode.GUIDANCE])

    def test_picker_launch_passes_ultracode_to_a_fake_claude(self):
        script = self.home / "fake claude.py"
        output = self.home / "args.json"
        script.write_text("import json, sys\nfrom pathlib import Path\n"
                          "Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))\n")
        self.config.write_text(json.dumps({"agents": {"Claude": {
            "argv": [sys.executable, str(script), str(output), "--model", "m"], "defaults": {"ultracode": True}}}}))
        env = dict(os.environ, AGENT_PICKER_NO_PRETRUST="1", HERDR_ENV="0")
        result = subprocess.run([sys.executable, str(ROOT / "src/agent_picker.py"), "--picker-launch", "Claude", "hi"],
                                cwd=self.home, env=env, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads(output.read_text())
        self.assertEqual(self.settings_of(args), {"ultracode": True, "enableWorkflows": True})
        self.assertEqual(self.flag(args, "--append-system-prompt"), [ultracode.GUIDANCE])
        self.assertIn("hi", args)


if __name__ == "__main__":
    unittest.main()
