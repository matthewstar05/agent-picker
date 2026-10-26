import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import agent_picker as ap
import grokbot
import grokbot_sessions
import handoff
import herdr_ai
import preferences
import session_refresh
import team
import usage_panel
from test_herdr_ai import FakeHerdr

BOT = "ad4d73c3-0fc4-47c3-82d8-c34054445af3"


class GrokBotHerdrTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.profile = next(a for a in ap.AGENTS if a["name"] == "Grok Bot")
        for context in (patch.dict(os.environ, AGENT_PICKER_STATE_HOME=str(self.root / "state")),
                        patch.object(preferences, "PATH", self.root / "preferences.json"),
                        patch.object(preferences, "_values", {}), patch.object(team, "SHARED", self.root / "notes")):
            context.start()
            self.addCleanup(context.stop)

    def test_setup_menu_accepts_cli_and_display_names_without_model_controls(self):
        with patch.object(ap, "installed", return_value=True):
            self.assertIn("Grok Bot", [a["name"] for a in team.setups()])
            for alias in ("grok", "grokbot", "grok-bot", "Grok Bot"):
                self.assertEqual(team.find(alias), "Grok Bot")
            for kind in (team.OFF, team.INDIVIDUAL, team.ORCHESTRATOR):
                with patch.object(team, "mode", return_value=kind):
                    # No model controls: its defaults screen holds only where it works.
                    self.assertEqual(team.models_menu("Grok Bot").fields(), ("where",))

    def test_fake_herdr_builds_all_modes_with_five_grokbot_launches(self):
        for kind in (team.OFF, team.INDIVIDUAL, team.ORCHESTRATOR):
            fake = FakeHerdr()
            with patch.object(herdr_ai.subprocess, "run", fake), patch.object(herdr_ai, "sidebar_update"), \
                 patch.dict(os.environ, HERDR_AI_DRY="0"):
                panes = herdr_ai.build(herdr_ai.Herdr("fake-herdr", "test"), "w9:p1", str(self.root),
                                       "Grok Bot", {"lead": "Grok Bot", "gemini": False, "mode": kind})
            runs = fake.runs()
            self.assertEqual(len(panes), 7)
            for role in herdr_ai.PEERS:
                words = shlex.split(runs[panes[role]][-1])
                at = words.index("--picker-launch")
                self.assertEqual(words[at + 1], "Grok Bot")
                prepared, args = team.prepare(self.profile, words[at + 2:])
                # Every generated role instruction fits the real CLI's parser.
                grokbot.parser().parse_args(prepared["argv"][1:] + args)
            self.assertIn("--picker-only 'Grok Bot'", runs[panes["usage"]][-1])

    def test_pinned_grokbot_usage_keeps_three_bars_on_one_cursor_card(self):
        now = 1800000000
        entry = {"fetched": now, "windows": [{"label": k, "pct": n, "resets": now + 86400}
                                            for k, n in (("cr", 10), ("ex", 20), ("gb", 30))]}
        with patch.dict(usage_panel.USAGE, {"Cursor": entry}, clear=True):
            view = usage_panel.View([self.profile], pinned=True)
            self.assertEqual([a["name"] for a in view.agents], ["Cursor"])
            text = ap._ANSI.sub("", "".join(usage_panel._card(view, 0, 66, 0, now, entry, False, 0, rows=4)))
            for label in ("cr", "ex", "gb"):
                self.assertIn(label, text)
            both = usage_panel.View([next(a for a in ap.AGENTS if a["name"] == "Cursor"), self.profile], pinned=True)
            self.assertEqual(len(both.agents), 1)

    def test_refresh_identifies_native_python_and_preserves_exact_saved_session(self):
        wrapper = {"pid": 1, "argv": [sys.executable, "/src/agent_picker.py", "--picker-launch", "Grok Bot"]}
        native = {"pid": 2, "name": "Python", "argv": [sys.executable, "/src/grokbot.py", "--workspace", str(self.root),
                                                             "--machine", "local", "--resume", "old", "old opening prompt"]}
        self.assertIs(session_refresh.native_process({"foreground_processes": [wrapper, native]}, "Grok Bot"), native)
        self.assertEqual(session_refresh.provider("grok"), "Grok Bot")
        expected = native["argv"][:6] + ["--resume", BOT]
        self.assertEqual(session_refresh.resume_argv("Grok Bot", native["argv"], BOT), expected)
        self.assertEqual(session_refresh.resume_argv("Grok Bot", ["grokbot", "old opening prompt"], BOT), ["grokbot", "--resume", BOT])
        self.assertEqual(session_refresh.resume_argv("Grok Bot", ["grokbot", "--append-system-prompt", "brief", "-r", "old"], BOT),
                         ["grokbot", "--append-system-prompt", "brief", "--resume", BOT])
        with self.assertRaises(ValueError):
            session_refresh.resume_argv("Grok Bot", ["grokbot", "-p", "old task"], BOT)
        grokbot_sessions.save({"id": BOT, "cwd": str(self.root), "messages": [], "pending": None})
        self.assertTrue(session_refresh.stored("Grok Bot", BOT, str(self.root)))
        self.assertFalse(session_refresh.stored("Grok Bot", BOT, str(self.root / "other")))

    def test_grokbot_handoff_uses_only_its_weekly_allowance(self):
        now = 1800000000
        reading = {"Cursor": {"fetched": now, "windows": [
            {"label": "cr", "pct": 1, "resets": now + 86400},
            {"label": "ex", "pct": 2, "resets": now + 86400},
            {"label": "gb", "pct": 95, "resets": now + 86400}]}}
        self.assertFalse(handoff.usable("Grok Bot", reading, now))
        reading["Cursor"]["windows"][-1]["pct"] = 5
        reading["Cursor"]["windows"][0]["pct"] = 100
        self.assertTrue(handoff.usable("Grok Bot", reading, now))

    def test_real_picker_peer_launch_uses_fake_executable_and_project_directory(self):
        fake = self.root / "fake-grokbot"
        output = self.root / "launch.json"
        fake.write_text("#!/usr/bin/env python3\nimport json,os,sys\nfrom pathlib import Path\n"
                        + "Path(" + repr(str(output)) + ").write_text(json.dumps({'argv':sys.argv[1:], 'cwd':os.getcwd()}))\n")
        fake.chmod(0o755)
        config = self.root / "config.json"
        config.write_text(json.dumps({"agents": {"Grok Bot": {"command": str(fake)}}}))
        env = dict(os.environ, AGENT_PICKER_CONFIG=str(config), HERDR_TAB_ID="fake:t")
        result = subprocess.run([sys.executable, str(ROOT / "src/agent_picker.py"), "--picker-launch", "Grok Bot",
                                 "--picker-peer", "fake:p1", "--picker-peers", "fake:p1,fake:p2"],
                                cwd=self.root, env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = json.loads(output.read_text())
        self.assertEqual(saved["cwd"], str(self.root))
        self.assertIn("nobody leads", saved["argv"][-1])
        # The brief is a standing instruction, like Claude's, not an opening chat turn.
        self.assertEqual(saved["argv"][-2], "--append-system-prompt")
        self.assertFalse(any(word.startswith("--picker-") for word in saved["argv"]))


if __name__ == "__main__":
    unittest.main()
