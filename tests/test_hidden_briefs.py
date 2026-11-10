"""Herdr briefs reach Antigravity and Cursor through folders, not the transcript."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import agy_prompts
import herdr_ai
import hidden_briefs
import provider_defaults
import session_refresh
import system_prompts


class HiddenBriefTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.state, self.temp = self.base / "state", self.base / "temp"
        for name, path in (("_state_root", self.state), ("_temp_root", self.temp)):
            root = patch.object(hidden_briefs, name, return_value=path)
            root.start()
            self.addCleanup(root.stop)

    def test_cursor_plugin_hook_returns_the_brief_as_hidden_context(self):
        brief = 'Panes: agent 1 = "a"; agent 2 = b.\nUnicode stays: café'
        flag, folder = hidden_briefs.attach("Cursor", brief)
        folder = Path(folder)
        self.assertEqual((flag, folder.parent), ("--plugin-dir", self.state))
        manifest = json.loads((folder / ".cursor-plugin/plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "agent-picker-brief")
        hooks = json.loads((folder / "hooks/hooks.json").read_text(encoding="utf-8"))
        command = hooks["hooks"]["sessionStart"][0]["command"]
        result = subprocess.run(command, shell=True, input='{"session_id": "s", "user_email": "x"}',
                                capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"additional_context": brief})
        self.assertNotIn("user_email", result.stdout)

    def test_same_brief_reuses_its_folder_and_another_gets_its_own(self):
        first = hidden_briefs.attach("Cursor", "one")
        self.assertEqual(hidden_briefs.attach("Cursor", "one"), first)
        self.assertNotEqual(hidden_briefs.attach("Cursor", "two"), first)
        self.assertEqual(sorted(p.name for p in self.state.iterdir() if p.is_dir()),
                         sorted([Path(first[1]).name, Path(hidden_briefs.attach("Cursor", "two")[1]).name]))

    def test_agy_rule_is_always_on_and_sorts_after_the_workspace(self):
        self.assertTrue(hidden_briefs.supported("Antigravity", cwd=str(self.base / "a-project"), brief="b"))
        self.assertFalse(hidden_briefs.supported("Antigravity", cwd=str(self.base / "zz-project"), brief="b"))
        self.assertFalse(hidden_briefs.supported("Antigravity", cwd=str(self.base / "a"), brief="x" * 30000))
        self.assertFalse(hidden_briefs.supported("Kiro", cwd=str(self.base / "a"), brief="b"))
        flag, folder = hidden_briefs.attach("Antigravity", "Herdr brief")
        self.assertEqual((flag, Path(folder).parent), ("--add-dir", self.temp))
        rule = (Path(folder) / ".agents/rules/herdr-brief.md").read_text(encoding="utf-8")
        self.assertTrue(rule.startswith("---\ntrigger: always_on\n---\nHerdr brief\n"))
        self.assertIn(hidden_briefs.WORKSPACE_NOTE, rule)

    def test_agy_off_profile_carries_the_brief_because_off_drops_rules(self):
        agent = {"name": "Antigravity", "argv": ["agy", "--model", "m"], "system_prompts": True}
        agent["defaults"] = {"system_prompts": False}
        flags = hidden_briefs.attach("Antigravity", "# Positions\nagent 2 = p2")
        argv = system_prompts.prepare(agent, ["agy", "--model", "m", *flags])
        self.assertEqual(argv[1:3], ["--agent", agy_prompts.BRIEF_NAME])
        self.assertEqual(system_prompts.prepare(agent, argv), argv)
        profile = (Path(flags[1]) / ".agents/agents" / (agy_prompts.BRIEF_NAME + ".md")).read_text(encoding="utf-8")
        self.assertIn("excludeDefaultComponents: true", profile)
        self.assertIn("name: " + agy_prompts.BRIEF_NAME + "\n", profile)
        self.assertTrue(profile.endswith("# herdr_brief\n## Positions\nagent 2 = p2\n"))
        with self.assertRaisesRegex(ValueError, "cannot be combined"):
            system_prompts.prepare(agent, ["agy", "--agent", "mine", *flags])

    def test_refresh_keeps_the_folder_and_rebuilds_a_cleared_one(self):
        flags = hidden_briefs.attach("Antigravity", "brief to keep")
        folder = Path(flags[1])
        for path in sorted(folder.rglob("*"), reverse=True):
            path.rmdir() if path.is_dir() else path.unlink()
        folder.rmdir()
        argv = session_refresh.resume_argv("Antigravity", ["agy", "--model", "m", *flags], "sid")
        self.assertEqual(argv, ["agy", "--model", "m", *flags, "--conversation", "sid"])
        self.assertEqual((folder / hidden_briefs.MARKER).read_text(encoding="utf-8"), "brief to keep")
        cursor = hidden_briefs.attach("Cursor", "cursor brief")
        argv = session_refresh.resume_argv("Cursor", ["cursor-agent", "--model", "m", *cursor, "old prompt"], "sid")
        self.assertEqual(argv, ["cursor-agent", "--model", "m", *cursor, "--resume", "sid"])

    def test_unused_folders_expire(self):
        old = hidden_briefs.attach("Cursor", "old")
        stale = time.time() - hidden_briefs.KEEP - 60
        for path in (Path(old[1]), self.state / (Path(old[1]).name + ".txt")):
            os.utime(path, (stale, stale))
        hidden_briefs.attach("Cursor", "new")
        self.assertFalse(Path(old[1]).exists())
        self.assertFalse((self.state / (Path(old[1]).name + ".txt")).exists())

    def test_handoff_waits_for_an_opening_turn_only_where_one_is_sent(self):
        cwd = str(self.base / "a-project")
        self.assertFalse(hidden_briefs.supported("Kiro", cwd))
        self.assertTrue(hidden_briefs.supported("Cursor", cwd))
        self.assertTrue(hidden_briefs.supported("Antigravity", cwd))
        self.assertIs(herdr_ai.hidden_briefs, hidden_briefs)


if __name__ == "__main__":
    unittest.main()
