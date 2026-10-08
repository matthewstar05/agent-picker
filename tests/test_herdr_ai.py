import json
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import herdr_ai

INSIDE = {"HERDR_ENV": "1", "HERDR_WORKSPACE_ID": "w9", "HERDR_TAB_ID": "w9:t1", "HERDR_PANE_ID": "w9:p1"}


class FakeHerdr:
    """Records herdr calls and answers with the JSON shapes herdr prints."""

    def __init__(self):
        self.calls = []
        self.next_id = 1
        # pane id -> {"agent": ..., "argv": [...]} for panes on the tab; None argv = idle shell.
        self.panes = {"w9:p1": {}}

    def __call__(self, argv, **kwargs):
        args = argv[1:]
        self.calls.append(args)
        if args[:2] in (["workspace", "create"], ["tab", "create"]):
            result = {"root_pane": {"pane_id": "w9:p1"}}
        elif args[:2] == ["pane", "split"]:
            self.next_id += 1
            pane = f"w9:p{self.next_id}"
            self.panes[pane] = {}
            result = {"pane": {"pane_id": pane}}
        elif args[:2] == ["pane", "list"]:
            result = {"panes": [{"pane_id": p, "tab_id": "w9:t1", "terminal_id": "term_" + p,
                                 **({"agent": i["agent"]} if i.get("agent") else {})}
                                for p, i in self.panes.items()] +
                               [{"pane_id": "w9:pX", "tab_id": "w9:t2", "agent": "codex"}]}
        elif args[:2] == ["pane", "process-info"]:
            argv_ = self.panes[args[3]].get("argv")
            result = {"process_info": {"shell_pid": 10, "foreground_process_group_id": 20 if argv_ else 10,
                                       "foreground_processes": [{"pid": 20, "argv": argv_}] if argv_ else []}}
        else:
            result = {}
        return SimpleNamespace(returncode=0, stdout=json.dumps({"result": result}), stderr="")

    def runs(self):
        out = {}
        for c in self.calls:
            if c[:2] == ["pane", "run"]:
                out.setdefault(c[2], []).append(c[3])
        return out

    def start_agents(self):
        """Pretend every pane the layout launched is running, so a reroute has work to do."""
        for pane, lines in self.runs().items():
            self.panes[pane]["argv"] = shlex.split(lines[-1])
        self.calls.clear()


class HerdrAiTests(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.TemporaryDirectory()
        self.addCleanup(self.state.cleanup)
        self.fake = FakeHerdr()

    def run_cli(self, env, argv=(), cwd=ROOT, entry=herdr_ai.main):
        clean = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
        with patch.dict(os.environ, {**clean, **env, "AGENT_PICKER_STATE_HOME": self.state.name}, clear=True), \
             patch.object(herdr_ai.subprocess, "run", self.fake), \
             patch.object(herdr_ai.subprocess, "call", return_value=0) as attach, \
             patch.object(herdr_ai.os, "execvp") as execvp, \
             patch.object(herdr_ai, "stop_panes") as stop:
            entry(list(argv), herdr="herdr", cwd=str(cwd))
        self.stopped = stop.call_args[0][1] if stop.called else []
        return attach.called or execvp.called

    def test_inside_herdr_lays_out_the_current_tab(self):
        attached = self.run_cli(INSIDE, ["demo"])
        created = [c for c in self.fake.calls if c[1] == "create"]
        self.assertEqual(created, [])
        self.assertEqual(self.fake.calls[0][:3], ["pane", "split", "w9:p1"])
        self.assertIn(["tab", "rename", "w9:t1", "demo"], self.fake.calls)
        self.assertFalse(attached)

    def test_caller_pane_is_swapped_into_the_terminal_slot(self):
        self.run_cli(INSIDE)
        swap = next(c for c in self.fake.calls if c[:2] == ["pane", "swap"])
        self.assertEqual(swap[2:4], ["--source-pane", "w9:p1"])
        self.assertNotIn("w9:p1", self.fake.runs())

    def test_outside_herdr_creates_workspace_and_attaches(self):
        attached = self.run_cli({})
        self.assertEqual(self.fake.calls[0][:2], ["workspace", "create"])
        self.assertIn(ROOT.name, self.fake.calls[0])
        self.assertTrue(attached)

    def test_every_pane_is_split_in_the_working_directory(self):
        self.run_cli(INSIDE)
        splits = [c for c in self.fake.calls if c[:2] == ["pane", "split"]]
        self.assertEqual(len(splits), 6)
        for call in splits:
            self.assertEqual(call[call.index("--cwd") + 1], str(ROOT))

    def test_panes_launch_agents_and_usage_through_the_picker(self):
        self.run_cli(INSIDE)
        commands = [c for lines in self.fake.runs().values() for c in lines]
        self.assertEqual(len(commands), 6)
        for name in ("Antigravity", "Codex", "Claude"):
            self.assertTrue(any(c.endswith("--picker-launch " + name) for c in commands), name)
        self.assertTrue(any(c.endswith("--picker-launch-priority") for c in commands))
        self.assertTrue(any(c.endswith("--picker-usage") for c in commands))

    def test_dry_run_builds_layout_without_launching(self):
        self.run_cli({**INSIDE, "HERDR_AI_DRY": "1"})
        self.assertEqual(self.fake.runs(), {})

    def test_rerun_in_a_laid_out_tab_reroutes_instead_of_splitting(self):
        self.run_cli(INSIDE, cwd=ROOT)
        self.fake.start_agents()
        self.run_cli(INSIDE, cwd=ROOT / "src")
        self.assertFalse([c for c in self.fake.calls if c[:2] == ["pane", "split"]])
        runs = self.fake.runs()
        self.assertEqual(len(runs), 5)   # four agents and the selector; usage keeps running
        self.assertEqual(len(self.stopped), 5)
        for lines in runs.values():
            self.assertEqual(lines[0], "cd " + shlex.quote(str(ROOT / "src")))
        launched = [lines[1] for lines in runs.values()]
        for name in ("Antigravity", "Codex", "Claude"):
            self.assertTrue(any(c.endswith("--picker-launch " + name) for c in launched), name)
        self.assertTrue(any(c.endswith("--picker-launch-priority") for c in launched))
        self.assertIn(["tab", "rename", "w9:t1", "src"], self.fake.calls)

    def test_explicit_label_survives_a_reroute(self):
        self.run_cli(INSIDE, ["demo"])
        self.fake.start_agents()
        self.run_cli(INSIDE, cwd=ROOT / "src")
        self.assertFalse([c for c in self.fake.calls if c[:2] == ["tab", "rename"]])

    def test_agent_route_restarts_every_agent_on_the_tab_only(self):
        self.fake.panes.update({
            "w9:p2": {"agent": "claude", "argv": ["claude", "--resume", "abc"]},
            "w9:p3": {"argv": ["npm", "run", "dev"]},          # not an agent: left alone
            "w9:p4": {"agent": "codex"},                         # agent pane already at its shell
        })
        self.run_cli(INSIDE, cwd=ROOT / "src", entry=herdr_ai.route_main)
        self.assertEqual(self.fake.runs(), {"w9:p2": ["cd " + shlex.quote(str(ROOT / "src")),
                                                      "claude --resume abc"]})
        self.assertEqual(self.stopped, ["w9:p2"])

    def test_agent_route_needs_herdr(self):
        with self.assertRaises(SystemExit):
            self.run_cli({}, entry=herdr_ai.route_main)

    def test_reset_closes_every_other_pane_and_forgets_the_layout(self):
        self.run_cli(INSIDE)
        self.fake.calls.clear()
        self.run_cli(INSIDE, cwd=ROOT / "src", entry=herdr_ai.reset_main)
        closed = [c[2] for c in self.fake.calls if c[:2] == ["pane", "close"]]
        self.assertEqual(len(closed), 6)
        self.assertNotIn("w9:p1", closed)
        self.assertIn(["tab", "rename", "w9:t1", "src"], self.fake.calls)
        for pane in closed:
            del self.fake.panes[pane]
        self.fake.calls.clear()
        self.run_cli(INSIDE)   # a fresh herdr-ai builds again instead of rerouting
        self.assertEqual(len([c for c in self.fake.calls if c[:2] == ["pane", "split"]]), 6)

    def test_reset_needs_herdr(self):
        with self.assertRaises(SystemExit):
            self.run_cli({}, entry=herdr_ai.reset_main)

    def test_busy_ignores_the_idle_shell(self):
        self.assertIsNone(herdr_ai.busy({"shell_pid": 5, "foreground_process_group_id": 5}))
        self.assertEqual(herdr_ai.busy({"shell_pid": 5, "foreground_process_group_id": 9}), 9)

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
