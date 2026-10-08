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

    def run_cli(self, env, argv=(), cwd=ROOT, entry=herdr_ai.main, setup=None):
        """setup is what the menu answers: None for Standard, a provider name, or False to quit."""
        clean = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
        self.menu = patch.object(herdr_ai, "pick_setup", side_effect=lambda name=None: setup if name is None
                                 else None if name == "standard" else name.capitalize())
        with patch.dict(os.environ, {**clean, **env, "AGENT_PICKER_STATE_HOME": self.state.name}, clear=True), \
             self.menu as menu, \
             patch.object(herdr_ai, "sidebar_update"), \
             patch.object(herdr_ai.subprocess, "run", self.fake), \
             patch.object(herdr_ai.subprocess, "call", return_value=0) as attach, \
             patch.object(herdr_ai.os, "execvp") as execvp, \
             patch.object(herdr_ai, "stop_panes") as stop:
            entry(list(argv), herdr="herdr", cwd=str(cwd))
        self.stopped = stop.call_args[0][1] if stop.called else []
        self.menu_calls = menu.call_args_list
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

    def test_outside_team_records_layout_identity(self):
        original = self.fake.__call__

        def respond(argv, **kwargs):
            result = original(argv, **kwargs)
            if argv[1:3] == ["workspace", "create"]:
                payload = json.loads(result.stdout)
                payload["result"]["workspace"] = {"workspace_id": "w9"}
                payload["result"]["tab"] = {"tab_id": "w9:t1"}
                result.stdout = json.dumps(payload)
            return result

        with patch.object(herdr_ai.herdr_sidebar, "endpoint", return_value="outside.sock"):
            # run_cli patches subprocess with the fake object, so give it the
            # real creation response shape for this outside-launch regression.
            with patch.object(FakeHerdr, "__call__", side_effect=respond):
                self.run_cli({}, setup="Codex")
        saved = json.loads((Path(self.state.name) / "herdr-ai-tabs.json").read_text())
        entry = saved["outside.sock|w9:t1"]
        self.assertEqual(entry["team"], "Codex")
        self.assertEqual(entry["terminals"][entry["panes"]["crew1"]], "term_" + entry["panes"]["crew1"])

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
            self.assertEqual(lines[0], herdr_ai.cd_command(str(ROOT / "src")))
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
        self.assertEqual(self.fake.runs(), {"w9:p2": [herdr_ai.cd_command(str(ROOT / "src")),
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

    def team_panes(self):
        return json.loads((Path(self.state.name) / "herdr-ai-tabs.json").read_text())["default|w9:t1"]

    def test_team_lays_out_a_lead_beside_four_crew(self):
        self.run_cli(INSIDE, setup="Claude")
        splits = [c for c in self.fake.calls if c[:2] == ["pane", "split"]]
        self.assertEqual(len(splits), 6)
        entry = self.team_panes()
        self.assertEqual(entry["team"], "Claude")
        panes = entry["panes"]
        self.assertEqual(set(panes), {"terminal", "lead", "usage", "crew1", "crew2", "crew3", "crew4"})
        self.assertEqual(panes["terminal"], "w9:p1")
        runs = self.fake.runs()
        self.assertNotIn("w9:p1", runs)
        crew = ",".join(panes[r] for r in herdr_ai.CREW)
        self.assertTrue(runs[panes["lead"]][0].endswith("--picker-launch Claude --picker-lead " + crew))
        for role in herdr_ai.CREW:
            self.assertTrue(runs[panes[role]][0].endswith("--picker-launch Claude --picker-crew " + panes["lead"]))
        self.assertTrue(runs[panes["usage"]][0].endswith("--picker-usage --picker-only Claude"))

    def test_team_usage_pane_is_short(self):
        self.run_cli(INSIDE, setup="Codex")
        panes = self.team_panes()["panes"]
        usage = next(c for c in self.fake.calls if c[:3] == ["pane", "split", panes["lead"]])
        self.assertEqual(usage[usage.index("--direction") + 1], "down")
        self.assertGreater(float(usage[usage.index("--ratio") + 1]), 0.75)

    def test_rerun_reroutes_a_team_with_its_own_commands(self):
        self.run_cli(INSIDE, setup="Kiro")
        panes = self.team_panes()["panes"]
        self.fake.start_agents()
        self.run_cli(INSIDE, cwd=ROOT / "src", setup="Codex")   # the menu is not asked on a reroute
        self.assertEqual(self.menu_calls, [])
        self.assertFalse([c for c in self.fake.calls if c[:2] == ["pane", "split"]])
        runs = self.fake.runs()
        self.assertEqual(set(runs), {panes[r] for r in ("lead", *herdr_ai.CREW)})   # usage keeps running
        self.assertTrue(runs[panes["lead"]][1].endswith(
            "--picker-launch Kiro --picker-lead " + ",".join(panes[r] for r in herdr_ai.CREW)))
        self.assertTrue(runs[panes["crew3"]][1].endswith("--picker-launch Kiro --picker-crew " + panes["lead"]))

    def test_agent_route_restarts_team_roles_with_their_commands(self):
        self.run_cli(INSIDE, setup="Claude")
        panes = self.team_panes()["panes"]
        self.fake.start_agents()
        self.run_cli(INSIDE, cwd=ROOT / "src", entry=herdr_ai.route_main)
        runs = self.fake.runs()
        self.assertTrue(runs[panes["crew1"]][1].endswith("--picker-crew " + panes["lead"]))
        self.assertNotIn(panes["usage"], runs)

    def test_quitting_the_menu_leaves_the_tab_alone(self):
        self.run_cli(INSIDE, setup=False)
        self.assertEqual(self.fake.calls, [])
        self.assertFalse((Path(self.state.name) / "herdr-ai-tabs.json").exists())

    def test_quitting_the_menu_outside_herdr_creates_nothing(self):
        attached = self.run_cli({}, setup=False)
        self.assertEqual(self.fake.calls, [])
        self.assertFalse(attached)

    def test_setup_flag_skips_the_menu(self):
        self.run_cli(INSIDE, ["--setup", "codex", "demo"])
        self.assertEqual([c.args for c in self.menu_calls], [("codex",)])
        self.assertEqual(self.team_panes()["team"], "Codex")
        self.assertIn(["tab", "rename", "w9:t1", "demo"], self.fake.calls)

    def test_setup_flag_standard_builds_the_workbench(self):
        self.run_cli(INSIDE, ["--setup=standard"])
        self.assertIsNone(self.team_panes()["team"])
        self.assertIn("picker", self.team_panes()["panes"])

    def test_setup_flag_on_a_tab_laid_out_differently_refuses(self):
        self.run_cli(INSIDE)
        self.fake.start_agents()
        with patch("team.find", return_value="Claude"), self.assertRaises(SystemExit) as stop:
            self.run_cli(INSIDE, ["--setup", "claude"])
        self.assertIn("laid out as Standard", str(stop.exception))
        self.assertEqual([c for c in self.fake.calls if c[:2] in (["pane", "run"], ["pane", "split"])], [])

    def test_team_without_its_lead_is_not_rerouted(self):
        self.run_cli(INSIDE, setup="Claude")
        lead = self.team_panes()["panes"]["lead"]
        self.fake.start_agents()
        del self.fake.panes[lead]
        with self.assertRaises(SystemExit) as stop:
            self.run_cli(INSIDE, cwd=ROOT / "src")
        self.assertIn("lead pane is gone", str(stop.exception))
        self.assertEqual(self.fake.runs(), {})

    def test_without_a_terminal_the_menu_defaults_to_standard(self):
        with patch.object(herdr_ai.sys.stdin, "isatty", return_value=False):
            self.assertIsNone(herdr_ai.pick_setup())

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
