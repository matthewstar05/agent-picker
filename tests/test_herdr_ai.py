import json
import os
import shlex
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import herdr_ai


class FakeHerdr:
    """Records herdr calls and answers with the JSON shapes herdr prints."""

    def __init__(self):
        self.calls = []
        self.next_id = 1

    def __call__(self, argv, **kwargs):
        args = argv[1:]
        self.calls.append(args)
        if args[:2] in (["workspace", "create"], ["tab", "create"]):
            result = {"root_pane": {"pane_id": "p1"}}
        elif args[:2] == ["pane", "split"]:
            self.next_id += 1
            result = {"pane": {"pane_id": f"p{self.next_id}"}}
        else:
            result = {}
        return SimpleNamespace(returncode=0, stdout=json.dumps({"result": result}), stderr="")

    def runs(self):
        return {c[2]: c[3] for c in self.calls if c[:2] == ["pane", "run"]}


class HerdrAiTests(unittest.TestCase):
    def run_layout(self, env, argv=()):
        fake = FakeHerdr()
        clean = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
        with patch.dict(os.environ, {**clean, **env}, clear=True), \
             patch.object(herdr_ai.subprocess, "run", fake), \
             patch.object(herdr_ai.subprocess, "call", return_value=0) as attach, \
             patch.object(herdr_ai.os, "execvp") as execvp:
            herdr_ai.main(list(argv), herdr="herdr", cwd=str(ROOT))
        return fake, attach.called or execvp.called

    def test_inside_herdr_opens_tab_in_current_workspace(self):
        fake, attached = self.run_layout({"HERDR_ENV": "1", "HERDR_WORKSPACE_ID": "w9"}, ["demo"])
        self.assertEqual(fake.calls[0][:4], ["tab", "create", "--workspace", "w9"])
        self.assertIn("demo", fake.calls[0])
        self.assertFalse(attached)

    def test_outside_herdr_creates_workspace_and_attaches(self):
        fake, attached = self.run_layout({})
        self.assertEqual(fake.calls[0][:2], ["workspace", "create"])
        self.assertIn(ROOT.name, fake.calls[0])
        self.assertTrue(attached)

    def test_every_pane_is_split_in_the_working_directory(self):
        fake, _ = self.run_layout({"HERDR_ENV": "1", "HERDR_WORKSPACE_ID": "w"})
        splits = [c for c in fake.calls if c[:2] == ["pane", "split"]]
        self.assertEqual(len(splits), 6)
        for call in splits:
            self.assertEqual(call[call.index("--cwd") + 1], str(ROOT))

    def test_panes_launch_agents_and_usage_through_the_picker(self):
        fake, _ = self.run_layout({"HERDR_ENV": "1", "HERDR_WORKSPACE_ID": "w"})
        commands = list(fake.runs().values())
        self.assertEqual(len(commands), 6)
        for name in ("Antigravity", "Codex", "Claude", "Cursor"):
            self.assertTrue(any(c.endswith("--picker-launch " + name) for c in commands), name)
        self.assertTrue(any(c.endswith("--picker-usage") for c in commands))

    def test_dry_run_builds_layout_without_launching(self):
        fake, _ = self.run_layout({"HERDR_ENV": "1", "HERDR_WORKSPACE_ID": "w", "HERDR_AI_DRY": "1"})
        self.assertEqual(fake.runs(), {})

    def test_posix_command_quotes_paths(self):
        with patch.object(herdr_ai, "WINDOWS", False), \
             patch.object(herdr_ai.sys, "executable", "/opt/my python/bin/python3"):
            words = shlex.split(herdr_ai.picker_command(["--picker-launch", "Codex"]))
        self.assertEqual(words[0], "/opt/my python/bin/python3")
        self.assertEqual(words[-2:], ["--picker-launch", "Codex"])

    def test_windows_command_works_in_powershell_and_cmd(self):
        with patch.object(herdr_ai, "WINDOWS", True), \
             patch.object(herdr_ai, "ENTRY", Path("C:/Projects/agent-picker/src/agent_picker.py")), \
             patch.object(herdr_ai.sys, "executable", r"C:\Python313\python.exe"):
            self.assertTrue(herdr_ai.picker_command(["--picker-usage"]).startswith(r"C:\Python313\python.exe "))
            with patch.object(herdr_ai.sys, "executable", r"C:\Users\Jane Doe\python.exe"):
                self.assertEqual(herdr_ai.picker_command(["--picker-launch", "Codex"]),
                                 "agent-picker.cmd --picker-launch Codex")


if __name__ == "__main__":
    unittest.main()
