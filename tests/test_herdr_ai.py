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
        self.label = "1"   # herdr numbers new tabs

    def __call__(self, argv, **kwargs):
        args = argv[1:]
        self.calls.append(args)
        if args[:2] in (["workspace", "create"], ["tab", "create"]):
            result = {"root_pane": {"pane_id": "w9:p1"}}
        elif args[:2] == ["tab", "get"]:
            result = {"tab": {"tab_id": args[2], "label": self.label}}
        elif args[:2] == ["tab", "rename"]:
            self.label = args[3]
            result = {}
        elif args[:2] == ["pane", "split"]:
            self.next_id += 1
            pane = f"w9:p{self.next_id}"
            self.panes[pane] = {}
            result = {"pane": {"pane_id": pane}}
        elif args[:2] == ["pane", "list"]:
            result = {"panes": [{"pane_id": p, "tab_id": "w9:t1", "terminal_id": "term_" + p,
                                 **{k: i[k] for k in ("agent", "agent_session", "agent_status", "cwd") if k in i}}
                                for p, i in self.panes.items()] +
                               [{"pane_id": "w9:pX", "tab_id": "w9:t2", "agent": "codex"}]}
        elif args[:2] == ["pane", "process-info"]:
            argv_ = self.panes[args[3]].get("argv")
            result = {"process_info": {"shell_pid": 10, "foreground_process_group_id": 20 if argv_ else 10,
                                       "foreground_processes": ([{"pid": 20, "argv": argv_}] +
                                           self.panes[args[3]].get("children", [])) if argv_ else []}}
        else:
            result = {}
        return SimpleNamespace(returncode=0, stdout=json.dumps({"result": result}), stderr="")

    def runs(self):
        """pane -> command lines started in it, by pane run or typed and submitted."""
        out = {}
        for c in self.calls:
            if c[:2] == ["pane", "run"]:
                out.setdefault(c[2], []).append(c[3])
            elif c[:2] == ["pane", "send-text"] and c[3].endswith("\n"):
                out.setdefault(c[2], []).append(c[3].lstrip("\x15")[:-1])
        return out

    def start_agents(self):
        """Pretend every pane the layout launched is running, so a reroute has work to do."""
        for pane, lines in self.runs().items():
            argv = shlex.split(lines[-1])
            self.panes[pane]["argv"] = argv
            if "--picker-launch" in argv:
                name = argv[argv.index("--picker-launch") + 1]
                self.start_agent(pane, argv, name)
            elif "--picker-launch-priority" in argv:
                self.start_agent(pane, argv, "Cursor")
            elif "--picker-relaunch" in argv:
                native = herdr_ai.session_refresh.decode(argv[argv.index("--picker-relaunch") + 1])
                self.panes[pane]["children"][0]["argv"] = native
        self.calls.clear()

    def start_agent(self, pane, wrapper, name):
        if name == "Star":
            role = wrapper[wrapper.index("--role") + 1] if "--role" in wrapper else "crew"
            who = wrapper[wrapper.index("--name") + 1] if "--name" in wrapper else role
            native = [sys.executable, str(ROOT / "src" / "starcli.py"), "--name", who, "--role", role]
            self.panes[pane].update(argv=wrapper, agent="star", agent_status="idle",
                agent_session={"agent": "star", "kind": "id", "value": "session-" + pane},
                children=[{"pid": 21, "argv": native, "argv0": "starcli.py", "name": "python"}])
            return
        binary = {"Claude": "claude", "Codex": "codex", "Cursor": "cursor-agent",
                  "Kiro": "kiro-cli", "Antigravity": "agy"}[name]
        native = ["/vendor/" + binary] + (["chat"] if name == "Kiro" else []) + ["--model", "saved-model"]
        if "--picker-lead" in wrapper or "--picker-crew" in wrapper:
            if name == "Claude":
                native += ["--append-system-prompt", "saved team instructions"]
            elif name == "Codex":
                native += ["-c", 'developer_instructions="saved team instructions"']
            elif "--picker-lead" in wrapper:
                native += ["-i", "old ready prompt"] if name == "Antigravity" else ["old ready prompt"]
        self.panes[pane].update(argv=wrapper, agent=binary, agent_status="idle",
            agent_session={"agent": binary, "kind": "id", "value": "session-" + pane},
            children=[{"pid": 21, "argv": native, "argv0": binary, "name": binary}])


class HerdrAiTests(unittest.TestCase):
    def test_star_refresh_fallback_preserves_native_busy_states_and_session_refs(self):
        receipt = {"agent_session": {"agent": "star", "kind": "id", "value": "local"},
                   "agent_status": "idle"}
        with patch.object(herdr_ai.session_refresh, "star_identity", return_value=receipt) as read:
            for state in ("working", "blocked"):
                result = herdr_ai.refresh_details("pane", {"agent_status": state}, {})
                self.assertEqual(result["agent_status"], state)
            native = {"agent_session": receipt["agent_session"], "agent_status": "idle"}
            read.reset_mock()
            self.assertEqual(herdr_ai.refresh_details("pane", native, {}), native)
            read.assert_called_once()
            receipt["agent_status"] = "working"
            self.assertEqual(herdr_ai.refresh_details("pane", native, {})["agent_status"], "working")
            receipt["agent_status"] = "idle"
            other = {**native, "agent_session": {"agent": "star", "kind": "id", "value": "other"}}
            result = herdr_ai.refresh_details("pane", other, {})
            self.assertEqual(result["agent_session"], other["agent_session"])
            self.assertEqual(result["agent_status"], "working")

    def test_star_refresh_uses_exact_local_receipts_when_herdr_omits_session_refs(self):
        import time
        self.run_cli(INSIDE, setup="Star")
        panes = self.team_panes()["panes"]
        self.fake.start_agents()
        members = Path(self.state.name) / "star/mesh/members"
        members.mkdir(parents=True)
        for role in ("lead", *herdr_ai.CREW):
            pane = panes[role]
            self.fake.panes[pane].pop("agent_session")
            (members / (role + ".json")).write_text(json.dumps({
                "pid": 21, "pane": pane, "tab": INSIDE["HERDR_TAB_ID"], "workspace": INSIDE["HERDR_WORKSPACE_ID"],
                "socket": None, "conversation": "local-" + role, "cwd": str(ROOT),
                "status": "idle", "updated": time.time()}))
        with patch.object(herdr_ai.session_refresh, "stored", return_value=True):
            self.run_cli(INSIDE, setup="Star")
        for role in ("lead", *herdr_ai.CREW):
            lines = self.fake.runs()[panes[role]]
            wrapper = shlex.split(lines[-1])
            native = herdr_ai.session_refresh.decode(wrapper[wrapper.index("--picker-relaunch") + 1])
            self.assertEqual(native[-2:], ["--resume-id", "local-" + role])

    def test_star_receipt_change_during_refresh_keeps_the_live_process(self):
        self.run_cli(INSIDE, setup="Star")
        panes = self.team_panes()["panes"]
        self.fake.start_agents()
        self.fake.panes[panes["lead"]].pop("agent_session")
        calls = []
        def identity(info, pane):
            if pane != panes["lead"]:
                return None
            calls.append(pane)
            return {"agent_session": {"agent": "star", "kind": "id", "value": "first" if len(calls) == 1 else "changed"},
                    "agent_status": "idle"}
        with patch.object(herdr_ai.session_refresh, "star_identity", side_effect=identity):
            self.run_cli(INSIDE, setup="Star")
        self.assertEqual(len(calls), 2)
        self.assertNotIn(panes["lead"], self.fake.runs())

    def test_reroute_resets_terminal_modes_before_changing_directory(self):
        self.assertEqual(herdr_ai.route_directory_command("/a b"),
                         "stty sane && printf '\\033c' && cd '/a b'")

    def test_reroute_types_control_bytes_as_raw_text(self):
        """send-keys would come out as CSI u sequences while a killed agent's
        kitty keyboard mode is still on, so nothing goes through send-keys."""
        self.run_cli(INSIDE, cwd=ROOT)
        codex = self.team_panes()["panes"]["codex"]
        self.fake.start_agents()
        self.run_cli(INSIDE, cwd=ROOT / "src")
        route = herdr_ai.route_directory_command(str(ROOT / "src"))
        typed = [c[3:] for c in self.fake.calls if c[:2] in (["pane", "send-keys"], ["pane", "send-text"]) and c[2] == codex]
        self.assertEqual(typed, [["\x03\x15"], ["\x15" + route + "\n"],
                                 ["\x15" + self.fake.runs()[codex][-1] + "\n"]])

    def test_stop_waits_for_children_after_wrapper_returns_to_shell(self):
        herdr = SimpleNamespace(process_info=lambda pane: {
            "shell_pid": 10, "foreground_process_group_id": 20 if not signals else 10})
        signals = []

        def killpg(pgid, sig):
            signals.append(sig)
            if sig == 0 and signals.count(0) == 2:
                raise ProcessLookupError

        with patch.object(herdr_ai.os, "getpgrp", return_value=99), \
             patch.object(herdr_ai.os, "killpg", side_effect=killpg), \
             patch.object(herdr_ai.time, "sleep"):
            herdr_ai.stop_panes(herdr, ["pane"])
        self.assertEqual(signals, [herdr_ai.signal.SIGTERM, 0, 0])

    def test_stop_treats_unreaped_group_as_stopped(self):
        # macOS reports EPERM for a group whose members are all zombies.
        polls = []
        herdr = SimpleNamespace(process_info=lambda pane: polls.append(pane) or {
            "shell_pid": 10, "foreground_process_group_id": 20 if len(polls) < 3 else 10})

        def killpg(pgid, sig):
            if sig == 0:
                raise PermissionError(1, "Operation not permitted")

        with patch.object(herdr_ai.os, "getpgrp", return_value=99), \
             patch.object(herdr_ai.os, "killpg", side_effect=killpg), \
             patch.object(herdr_ai.time, "sleep"):
            herdr_ai.stop_panes(herdr, ["pane"])

    def test_stop_aborts_if_pane_stays_busy(self):
        herdr = SimpleNamespace(process_info=lambda pane: {
            "shell_pid": 10, "foreground_process_group_id": 20})
        with patch.object(herdr_ai.os, "getpgrp", return_value=99), \
             patch.object(herdr_ai.os, "killpg", side_effect=ProcessLookupError), \
             patch.object(herdr_ai.time, "sleep"), \
             self.assertRaisesRegex(SystemExit, "no restart commands"):
            herdr_ai.stop_panes(herdr, ["pane"])

    @staticmethod
    def joined(argv):
        """A relaunch line the way relaunch_line writes it on this platform."""
        return shlex.join(argv)

    def setUp(self):
        self.state = tempfile.TemporaryDirectory()
        self.addCleanup(self.state.cleanup)
        self.fake = FakeHerdr()
        # Teams lead themselves without Gemini unless a test asks; no live usage reads.
        plan = patch.object(herdr_ai, "team_plan", side_effect=lambda team, mode=None: {"lead": team, "gemini": False} if team else {})
        self.team_plan = plan.start()
        self.addCleanup(plan.stop)
        # Fake sessions have no transcripts on disk; tests opt into that check.
        stored = patch.object(herdr_ai.session_refresh, "stored", return_value=None)
        self.stored_check = stored.start()
        self.addCleanup(stored.stop)
        # Codex's config check runs the real codex binary; tests opt into it.
        self.codex_patch = patch.object(herdr_ai, "codex_problem", return_value=None)
        self.codex_check = self.codex_patch.start()
        self.addCleanup(self.codex_patch.stop)
        # Handoff is off by default; the handoff tests run with it on.
        on = patch("handoff.enabled", return_value=True)
        self.handoff_on = on.start()
        self.addCleanup(on.stop)

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
            self.assertTrue(any(c.endswith("--picker-launch " + name + " --picker-solo") for c in commands), name)
        self.assertTrue(any(c.endswith("--picker-launch-priority --picker-solo") for c in commands))
        self.assertTrue(any(c.endswith("--picker-usage") for c in commands))

    def test_dry_run_builds_layout_without_launching(self):
        self.run_cli({**INSIDE, "HERDR_AI_DRY": "1"})
        self.assertEqual(self.fake.runs(), {})

    def test_dry_refresh_never_stops_existing_agents_or_clears_their_sessions(self):
        self.run_cli(INSIDE)
        self.fake.start_agents()
        self.run_cli({**INSIDE, "HERDR_AI_DRY": "1"})
        self.assertEqual(self.stopped, [])
        self.assertEqual(self.fake.runs(), {})

    def resumed_argv(self, pane):
        line = self.fake.runs()[pane][-1]
        args = shlex.split(line)
        return herdr_ai.session_refresh.decode(args[args.index("--picker-relaunch") + 1])

    def test_rerun_in_a_laid_out_tab_resumes_exact_sessions_without_splitting(self):
        self.run_cli(INSIDE, cwd=ROOT)
        self.fake.start_agents()
        self.run_cli(INSIDE, cwd=ROOT / "src")
        self.assertFalse([c for c in self.fake.calls if c[:2] == ["pane", "split"]])
        runs = self.fake.runs()
        self.assertEqual(len(runs), 6)   # agents, selector, and refreshed usage panel
        self.assertEqual(len(self.stopped), 6)
        for lines in runs.values():
            self.assertEqual(lines[0], herdr_ai.route_directory_command(str(ROOT / "src")))
        panes = self.team_panes()["panes"]
        for role, flag in (("agy", "--conversation"), ("codex", "resume"),
                           ("claude", "--resume"), ("cursor", "--resume")):
            pane = panes[role]
            native = self.resumed_argv(pane)
            self.assertEqual(native[-2:], [flag, "session-" + pane])
            self.assertIn("saved-model", native)
            self.assertIn("--picker-return", runs[pane][-1])
        self.assertTrue(runs[panes["usage"]][-1].endswith("--picker-usage"))
        self.assertIn(["tab", "rename", "w9:t1", "src"], self.fake.calls)

    def test_moving_directory_starts_fresh_where_a_conversation_cannot_follow(self):
        self.run_cli(INSIDE, cwd=ROOT)
        self.fake.start_agents()
        panes = self.team_panes()["panes"]
        for pane in panes.values():
            self.fake.panes[pane]["cwd"] = str(ROOT)
        del self.fake.panes[panes["codex"]]["agent_session"]
        self.stored_check.side_effect = lambda name, session, cwd: False if name == "Claude" else None
        self.run_cli(INSIDE, cwd=ROOT / "src")
        runs = self.fake.runs()
        for role in ("claude", "codex"):
            self.assertEqual(runs[panes[role]][-1], herdr_ai.picker_command(herdr_ai.PANES[role]))
        self.assertEqual(self.resumed_argv(panes["cursor"])[-2:], ["--resume", "session-" + panes["cursor"]])

    def test_same_directory_keeps_a_claude_pane_without_a_transcript(self):
        self.run_cli(INSIDE, cwd=ROOT)
        self.fake.start_agents()
        panes = self.team_panes()["panes"]
        for pane in panes.values():
            self.fake.panes[pane]["cwd"] = str(ROOT)
        self.stored_check.side_effect = lambda name, session, cwd: False if name == "Claude" else None
        self.run_cli(INSIDE, cwd=ROOT)
        self.assertNotIn(panes["claude"], self.fake.runs())

    def test_reroute_keeps_a_provider_switched_through_the_selector(self):
        self.run_cli(INSIDE, cwd=ROOT)
        panes = self.team_panes()["panes"]
        self.fake.start_agents()
        switched = [sys.executable, str(herdr_ai.ENTRY), "--picker-return", "--picker-launch", "Claude"]
        self.fake.start_agent(panes["codex"], switched, "Claude")
        self.run_cli(INSIDE, cwd=ROOT / "src")
        self.assertEqual(self.resumed_argv(panes["codex"])[0], "/vendor/claude")
        self.assertEqual(self.resumed_argv(panes["codex"])[-2:], ["--resume", "session-" + panes["codex"]])

    def test_reroute_keeps_a_switched_provider_herdr_does_not_recognize(self):
        self.run_cli(INSIDE, cwd=ROOT)
        panes = self.team_panes()["panes"]
        self.fake.start_agents()
        switched = [sys.executable, str(herdr_ai.ENTRY), "--picker-return", "--picker-launch", "Kiro"]
        self.fake.start_agent(panes["claude"], switched, "Kiro")
        self.fake.panes[panes["claude"]].pop("agent")  # detected through the process instead
        self.run_cli(INSIDE, cwd=ROOT / "src")
        self.assertEqual(self.resumed_argv(panes["claude"])[-2:], ["--resume-id", "session-" + panes["claude"]])

    def test_relayout_closes_the_old_usage_pane(self):
        self.run_cli(INSIDE, cwd=ROOT)
        panes = self.team_panes()["panes"]
        for role, pane in panes.items():
            if role not in ("terminal", "usage"):
                del self.fake.panes[pane]
        self.fake.calls.clear()
        self.run_cli(INSIDE, cwd=ROOT)
        self.assertIn(["pane", "close", panes["usage"]], self.fake.calls)
        self.assertNotEqual(self.team_panes()["panes"]["usage"], panes["usage"])

    def test_malformed_state_is_ignored(self):
        for text in ("[]", "null", '{"default|w9:t1": {"panes": {"claude": "w9:p1"}}}', '{"x": 3}'):
            with self.subTest(text=text):
                self.setUp()
                (Path(self.state.name) / "herdr-ai-tabs.json").write_text(text)
                self.run_cli(INSIDE, cwd=ROOT)
                self.assertIn("usage", self.team_panes()["panes"])

    def test_reroute_restores_a_usage_pane_that_left_the_usage_panel(self):
        selector = [sys.executable, str(herdr_ai.ENTRY)]
        for argv in (None, selector):   # quit to its shell, or a stale `usage` alias opened the selector
            with self.subTest(argv=argv):
                self.setUp()
                self.run_cli(INSIDE, cwd=ROOT)
                usage = self.team_panes()["panes"]["usage"]
                self.fake.start_agents()
                self.fake.panes[usage] = {"argv": argv}
                self.run_cli(INSIDE, cwd=ROOT / "src")
                self.assertTrue(self.fake.runs()[usage][-1].endswith("--picker-usage"))

    def test_reroute_leaves_other_programs_in_the_usage_pane_alone(self):
        self.run_cli(INSIDE, cwd=ROOT)
        usage = self.team_panes()["panes"]["usage"]
        self.fake.start_agents()
        self.fake.panes[usage] = {"argv": ["htop"]}
        self.run_cli(INSIDE, cwd=ROOT / "src")
        self.assertNotIn(usage, self.fake.runs())

    def test_explicit_label_survives_a_reroute(self):
        self.run_cli(INSIDE, ["demo"])
        self.fake.start_agents()
        self.run_cli(INSIDE, cwd=ROOT / "src")
        self.assertFalse([c for c in self.fake.calls if c[:2] == ["tab", "rename"]])

    def test_tab_renamed_in_herdr_keeps_its_name(self):
        self.fake.label = "api work"
        self.run_cli(INSIDE, cwd=ROOT)
        self.assertFalse([c for c in self.fake.calls if c[:2] == ["tab", "rename"]])

    def test_tab_renamed_in_herdr_after_layout_keeps_its_name(self):
        self.run_cli(INSIDE, cwd=ROOT)
        self.fake.start_agents()
        self.fake.label = "api work"
        self.run_cli(INSIDE, cwd=ROOT / "src")
        self.run_cli(INSIDE, cwd=ROOT, entry=herdr_ai.route_main)
        self.run_cli(INSIDE, cwd=ROOT, entry=herdr_ai.reset_main)
        self.assertFalse([c for c in self.fake.calls if c[:2] == ["tab", "rename"]])
        self.assertEqual(self.fake.label, "api work")

    def test_agent_route_follows_the_directory_it_named(self):
        self.run_cli(INSIDE, cwd=ROOT)
        self.fake.start_agents()
        self.run_cli(INSIDE, cwd=ROOT / "src", entry=herdr_ai.route_main)
        self.assertEqual(self.fake.label, "src")

    def test_agent_route_restarts_every_agent_on_the_tab_only(self):
        self.fake.panes.update({
            "w9:p2": {"agent": "claude", "argv": ["claude", "--resume", "abc"]},
            "w9:p3": {"argv": ["npm", "run", "dev"]},          # not an agent: left alone
            "w9:p4": {"agent": "codex"},                         # agent pane already at its shell
        })
        self.run_cli(INSIDE, cwd=ROOT / "src", entry=herdr_ai.route_main)
        self.assertEqual(self.fake.runs(), {"w9:p2": [herdr_ai.route_directory_command(str(ROOT / "src")),
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
        usage = " --picker-usage-pane " + panes["usage"]
        self.assertTrue(runs[panes["lead"]][0].endswith("--picker-launch Claude --picker-lead " + crew + usage))
        for role in herdr_ai.CREW:
            self.assertTrue(runs[panes[role]][0].endswith(
                "--picker-launch Claude --picker-crew " + panes["lead"] + usage))
        self.assertTrue(runs[panes["usage"]][0].endswith("--picker-usage --picker-only Claude"))

    def test_gemini_team_is_led_by_another_provider(self):
        self.team_plan.side_effect = lambda team, mode=None: {"lead": "Codex", "gemini": False}
        self.run_cli(INSIDE, setup="Antigravity")
        entry = self.team_panes()
        panes, runs = entry["panes"], self.fake.runs()
        self.assertEqual((entry["team"], entry["lead"]), ("Antigravity", "Codex"))
        self.assertIn("--picker-launch Codex --picker-lead", runs[panes["lead"]][0])
        self.assertIn("--picker-crew-of Antigravity", runs[panes["lead"]][0])
        for role in herdr_ai.CREW:
            self.assertIn("--picker-launch Antigravity --picker-crew", runs[panes[role]][0])
        self.assertTrue(runs[panes["usage"]][0].endswith("--picker-only Codex,Antigravity"))

    def test_refresh_resumes_idle_star_clis_and_leaves_a_working_one(self):
        self.run_cli(INSIDE, setup="Star")
        panes = self.team_panes()["panes"]
        self.fake.start_agents()
        self.fake.panes[panes["crew2"]]["agent_status"] = "working"
        self.run_cli(INSIDE, setup="Star")
        self.assertNotIn(panes["crew2"], self.stopped)
        self.assertIn(panes["usage"], self.stopped)
        for role in ("lead", "crew1", "crew3", "crew4"):
            native = self.resumed_argv(panes[role])
            self.assertTrue(str(native[1]).endswith("starcli.py"), native)
            self.assertEqual(native[native.index("--name") + 1], role if role == "lead" else role)
            self.assertIn("--role", native)
            self.assertEqual(native[-2:], ["--resume-id", "session-" + panes[role]])
            self.assertNotIn("--goal", native)

    def test_agent_route_restarts_a_star_mesh_in_the_new_directory(self):
        self.run_cli(INSIDE, setup="Star")
        panes = self.team_panes()["panes"]
        self.fake.start_agents()
        self.run_cli(INSIDE, cwd=ROOT / "src", entry=herdr_ai.route_main)
        runs = self.fake.runs()
        for role in ("lead", "crew1", "crew2", "crew3", "crew4"):
            cd, line = runs[panes[role]][-2:]
            self.assertIn(str(ROOT / "src"), cd)
            # A fresh member with its mesh name and role, not the old conversation.
            self.assertIn("--picker-launch Star", line)
            self.assertIn("--name " + role, line)
            self.assertIn("--role " + ("lead" if role == "lead" else "crew"), line)
            self.assertNotIn("--resume-id", line)
        self.assertEqual(self.team_panes()["team"], "Star")

    def test_handoff_from_a_star_mesh_keeps_the_panes(self):
        self.run_cli(INSIDE, setup="Star")
        panes = self.team_panes()["panes"]
        self.fake.start_agents()
        error, jobs = self.handoff(panes["lead"])
        self.assertIn("Star mesh stays", error)
        self.assertEqual(jobs, [])

    def test_crew_command_panes_join_a_star_layout(self):
        env = {**INSIDE, "HERDR_AI_DRY": "1", "HERDR_SOCKET_PATH": "",
               "AGENT_PICKER_STATE_HOME": self.state.name}
        with patch.dict(os.environ, env, clear=False):
            reason = herdr_ai.note_star_panes("w9:p9", {"crew1": "w9:c1", "crew2": "w9:c2"},
                                              {"w9:p9": "term_lead", "w9:c1": "term_c1", "w9:c2": "term_c2"})
        self.assertIsNone(reason)
        entry = self.team_panes()
        self.assertEqual(entry["team"], "Star")
        self.assertEqual(entry["panes"]["lead"], "w9:p9")
        self.assertEqual(entry["panes"]["crew1"], "w9:c1")
        self.assertEqual(entry["terminals"]["w9:c1"], "term_c1")
        self.assertEqual(herdr_ai.star_member_args("crew1", "crew")[:3],
                         ["--picker-return", "--picker-launch", "Star"])

    def test_crew_command_does_not_replace_a_standard_layout(self):
        self.run_cli(INSIDE)
        env = {**INSIDE, "HERDR_AI_DRY": "1", "HERDR_SOCKET_PATH": "",
               "AGENT_PICKER_STATE_HOME": self.state.name}
        with patch.dict(os.environ, env, clear=False):
            reason = herdr_ai.note_star_panes("w9:p9", {"crew1": "w9:c1"}, {"w9:p9": "term_lead", "w9:c1": "term_c1"})
        self.assertIn("Standard", reason)
        self.assertIsNone(self.team_panes()["team"])

    def test_crew_command_keeps_an_existing_crew_slot(self):
        self.run_cli(INSIDE, setup="Star")
        entry = self.team_panes()
        old = entry["panes"]["crew1"]
        env = {**INSIDE, "HERDR_AI_DRY": "1", "HERDR_SOCKET_PATH": "",
               "AGENT_PICKER_STATE_HOME": self.state.name}
        with patch.dict(os.environ, env, clear=False):
            reason = herdr_ai.note_star_panes(entry["panes"]["lead"], {"crew1": "w9:new", "crew5": "w9:extra"},
                                              {entry["panes"]["lead"]: entry["terminals"][entry["panes"]["lead"]],
                                               "w9:extra": "term_extra"})
        self.assertIsNone(reason)
        saved = self.team_panes()["panes"]
        self.assertEqual(saved["crew1"], old)
        self.assertEqual(saved["crew5"], "w9:extra")

    def test_a_usage_pane_attaches_only_to_an_existing_star_layout(self):
        env = {**INSIDE, "HERDR_AI_DRY": "1", "HERDR_SOCKET_PATH": "",
               "AGENT_PICKER_STATE_HOME": self.state.name}
        with patch.dict(os.environ, env, clear=False):
            self.assertIsNone(herdr_ai.note_star_panes("w9:p9", {}, {"w9:p9": "term_lead"}, usage="w9:usage", create=False))
        self.assertFalse((Path(self.state.name) / "herdr-ai-tabs.json").exists())
        with patch.dict(os.environ, env, clear=False):
            herdr_ai.note_star_panes("w9:p9", {"crew1": "w9:c1"}, {"w9:p9": "term_lead", "w9:c1": "term_c1"})
            self.assertIsNone(herdr_ai.note_star_panes("w9:p9", {}, {"w9:usage": "term_usage"}, usage="w9:usage", create=False))
        self.assertEqual(self.team_panes()["panes"]["usage"], "w9:usage")

    def test_quitting_a_star_pane_reopens_the_selector(self):
        panes = {"lead": "w:p2", "usage": "w:p3", **{r: f"w:c{i}" for i, r in enumerate(herdr_ai.CREW)}}
        commands = herdr_ai.layout_commands(herdr_ai.STAR, panes)
        for role in ("lead", *herdr_ai.CREW):
            self.assertEqual(commands[role][:3], ["--picker-return", "--picker-launch", "Star"], role)

    def test_other_teams_give_crew4_to_gemini(self):
        self.team_plan.side_effect = lambda team, mode=None: {"lead": team, "gemini": True}
        self.run_cli(INSIDE, setup="Claude")
        entry = self.team_panes()
        panes, runs = entry["panes"], self.fake.runs()
        self.assertTrue(entry["gemini"])
        self.assertIn("--picker-gemini " + panes["crew4"], runs[panes["lead"]][0])
        self.assertIn("--picker-launch Antigravity --picker-crew", runs[panes["crew4"]][0])
        for role in ("crew1", "crew2", "crew3"):
            self.assertIn("--picker-launch Claude --picker-crew", runs[panes[role]][0])
        self.assertTrue(runs[panes["usage"]][0].endswith("--picker-only Claude,Antigravity"))

    def handoff(self, pane, target=("Codex", "20% used"), argv=None):
        notes = Path(self.state.name) / "notes.md"
        notes.write_text("done: a\nleft: b\n")
        jobs = []
        env = {**INSIDE, "HERDR_PANE_ID": pane}
        with patch("handoff.target", return_value=target), \
             patch.object(herdr_ai, "spawn_runner", side_effect=lambda job, log: jobs.append(job)):
            try:
                self.run_cli(env, argv or ["--handoff", "Ship the parser\nfix", str(notes)])
            except SystemExit as exc:
                return str(exc), jobs
        return None, jobs

    def test_handoff_from_a_standard_pane_switches_only_that_pane(self):
        self.run_cli(INSIDE)
        panes = self.team_panes()["panes"]
        self.fake.start_agents()
        _, jobs = self.handoff(panes["claude"])
        job = jobs[0]
        self.assertEqual(list(job["lines"]), [panes["claude"]])
        self.assertTrue(job["lines"][panes["claude"]].endswith("--picker-return --picker-launch Codex --picker-solo"))
        self.assertTrue(job["goal"].startswith("/goal Ship the parser fix (Handed off from Claude"))
        self.assertIn("notes.md", job["goal"])
        self.assertFalse(job["opening"])

    def test_team_lead_handoff_moves_a_team_that_shares_its_quota(self):
        self.team_plan.side_effect = lambda team, mode=None: {"lead": team, "gemini": True}
        self.run_cli(INSIDE, setup="Claude")
        panes = self.team_panes()["panes"]
        self.fake.start_agents()
        _, jobs = self.handoff(panes["lead"], ("Kiro", "resets sooner"))
        job = jobs[0]
        moved = {panes[r] for r in ("lead", "usage", "crew1", "crew2", "crew3")}
        self.assertEqual(set(job["lines"]), moved)   # Gemini's crew4 keeps working
        self.assertIn("--picker-launch Kiro --picker-lead", job["lines"][panes["lead"]])
        self.assertIn("--picker-launch Kiro --picker-crew", job["lines"][panes["crew1"]])
        self.assertEqual(job["update"], {"team": "Kiro", "lead": "Kiro"})
        self.assertTrue(job["opening"])

    def test_gemini_team_handoff_replaces_only_the_lead(self):
        self.team_plan.side_effect = lambda team, mode=None: {"lead": "Codex", "gemini": False}
        self.run_cli(INSIDE, setup="Antigravity")
        panes = self.team_panes()["panes"]
        self.fake.start_agents()
        _, jobs = self.handoff(panes["lead"], ("Claude", "ok"))
        job = jobs[0]
        self.assertEqual(set(job["lines"]), {panes["lead"], panes["usage"]})
        self.assertIn("--picker-launch Claude --picker-lead", job["lines"][panes["lead"]])
        self.assertIn("--picker-crew-of Antigravity", job["lines"][panes["lead"]])
        self.assertEqual(job["update"], {"team": "Antigravity", "lead": "Claude"})

    def individual(self):
        self.team_plan.side_effect = lambda team, mode=None: {"lead": team, "gemini": False, "mode": "individual"}
        self.run_cli(INSIDE, setup="Claude")
        return self.team_panes()

    def test_individual_mode_runs_five_peers_of_one_provider(self):
        entry = self.individual()
        panes, runs = entry["panes"], self.fake.runs()
        self.assertEqual(entry["mode"], "individual")
        peers = ",".join(panes[r] for r in herdr_ai.PEERS)
        for role in herdr_ai.PEERS:
            self.assertIn(f"--picker-return --picker-launch Claude --picker-peer {panes[role]} --picker-peers {peers}",
                          runs[panes[role]][0], role)
            self.assertNotIn("--picker-crew", runs[panes[role]][0])
        self.assertTrue(runs[panes["usage"]][0].endswith("--picker-only Claude"))

    def test_individual_handoff_moves_only_that_peer(self):
        panes = self.individual()["panes"]
        self.fake.start_agents()
        _, jobs = self.handoff(panes["crew2"], ("Codex", "ok"))
        job = jobs[0]
        self.assertEqual(set(job["lines"]), {panes["crew2"], panes["usage"]})
        self.assertIn(f"--picker-launch Codex --picker-peer {panes['crew2']}", job["lines"][panes["crew2"]])
        self.assertTrue(job["lines"][panes["usage"]].endswith("--picker-only Claude,Codex"))
        self.assertEqual(job["update"], {"providers": {"crew2": "Codex"}})
        commands = herdr_ai.entry_commands({**self.team_panes(), **job["update"]})
        self.assertIn("Codex", commands["crew2"])
        self.assertIn("Claude", commands["crew1"])

    def test_system_prompt_off_runs_five_plain_agents(self):
        self.team_plan.side_effect = lambda team, mode=None: {"lead": team, "gemini": False, "mode": "off"}
        self.run_cli(INSIDE, setup="Claude")
        entry = self.team_panes()
        panes, runs = entry["panes"], self.fake.runs()
        self.assertEqual(entry["mode"], "off")
        for role in herdr_ai.PEERS:
            self.assertTrue(runs[panes[role]][0].endswith("--picker-return --picker-launch Claude --picker-plain"), role)
        self.assertTrue(runs[panes["usage"]][0].endswith("--picker-only Claude"))
        self.fake.start_agents()
        _, jobs = self.handoff(panes["crew1"], ("Codex", "ok"))
        self.assertTrue(jobs[0]["lines"][panes["crew1"]].endswith("--picker-launch Codex --picker-plain"))

    def test_mode_flag_refuses_a_tab_laid_out_in_the_other_mode(self):
        self.individual()
        with self.assertRaises(SystemExit) as raised:
            self.run_cli(INSIDE, ["--setup", "claude", "--mode", "orchestrator"])
        self.assertIn("is individual, not orchestrator", str(raised.exception))
        with self.assertRaises(SystemExit) as raised:
            self.run_cli(INSIDE, ["--mode", "solo"])
        self.assertIn("individual or orchestrator", str(raised.exception))

    def test_crew_and_exhausted_handoffs_are_refused(self):
        self.run_cli(INSIDE, setup="Claude")
        panes = self.team_panes()["panes"]
        self.fake.start_agents()
        error, jobs = self.handoff(panes["crew2"])
        self.assertIn("crew do not hand off", error)
        self.assertEqual(jobs, [])
        notes = Path(self.state.name) / "n.md"
        notes.write_text("x")
        out = []
        with patch("handoff.target", return_value=(None, "every provider is at 90%")), \
             patch.object(herdr_ai, "spawn_runner") as spawn, \
             patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
            self.run_cli({**INSIDE, "HERDR_PANE_ID": panes["lead"]}, ["--handoff", "g", str(notes)])
        spawn.assert_not_called()
        self.assertIn("Tell the user", out[-1])

    def test_handoff_is_refused_while_the_setting_is_off(self):
        self.run_cli(INSIDE)
        panes = self.team_panes()["panes"]
        self.fake.start_agents()
        self.handoff_on.return_value = False
        error, jobs = self.handoff(panes["claude"])
        self.assertIn("handoff is off", error)
        self.assertEqual(jobs, [])

    def test_handoff_needs_its_notes_file(self):
        self.run_cli(INSIDE)
        error, jobs = self.handoff("w9:p2", argv=["--handoff", "goal", "/nonexistent/notes.md"])
        self.assertIn("notes not found", error)

    def test_handoff_runner_waits_switches_and_sends_the_goal(self):
        self.run_cli(INSIDE)
        job = {"herdr": "herdr", "caller": "w9:p3", "lead": "w9:p3", "cwd": "/repo",
               "lines": {"w9:p3": "agent --picker-launch Codex"}, "key": "default|w9:t1",
               "update": {}, "goal": "/goal finish it", "opening": False}
        with patch.dict(os.environ, {"AGENT_PICKER_STATE_HOME": self.state.name}), \
             patch.object(herdr_ai.subprocess, "run", self.fake), \
             patch.object(herdr_ai, "stop_panes") as stop:
            self.assertEqual(herdr_ai.handoff_run(job, sleep=lambda s: None), 0)
        stop.assert_called_once()
        self.assertEqual(stop.call_args[0][1], ["w9:p3"])
        waits = [c for c in self.fake.calls if c[:2] == ["agent", "wait"]]
        self.assertEqual(waits[0][2], "w9:p3")
        self.assertIn("idle", waits[0])
        self.assertIn(["agent", "prompt", "w9:p3", "/goal finish it"], self.fake.calls)
        self.assertTrue(any("agent --picker-launch Codex" in t for t in self.fake.runs().get("w9:p3", [])))

    def test_a_codex_config_error_stops_the_layout_before_any_pane(self):
        self.codex_check.return_value = "unknown variant `never` in mcp_servers.iMCP.tools.x.approval_mode"
        with self.assertRaises(SystemExit) as exit:
            self.run_cli(INSIDE, setup="Codex")
        self.assertIn("unknown variant `never`", str(exit.exception))
        self.assertFalse([c for c in self.fake.calls if c[:2] == ["pane", "split"]])
        self.codex_check.assert_called_with("Codex", "Codex")

    def test_teams_without_codex_skip_the_codex_check(self):
        with patch.object(herdr_ai.subprocess, "run") as run:
            self.assertIsNone(herdr_ai.codex_problem("Claude", "Claude"))
        run.assert_not_called()

    def test_codex_check_reports_codex_stderr(self):
        failed = SimpleNamespace(returncode=1, stdout="", stderr="Error: failed to load bootstrap configuration\n\n"
                                 "Caused by:\n    unknown variant `never`\n")
        self.codex_patch.stop()
        try:
            with patch.object(herdr_ai.subprocess, "run", return_value=failed) as run, \
                 patch("platform_runtime.resolve_argv", return_value=["/vendor/codex"]):
                problem = herdr_ai.codex_problem(None)
        finally:
            self.codex_patch.start()
        self.assertEqual(run.call_args[0][0], ["/vendor/codex", "mcp", "list"])
        self.assertIn("unknown variant `never`", problem)

    def test_team_usage_pane_is_short(self):
        self.run_cli(INSIDE, setup="Codex")
        panes = self.team_panes()["panes"]
        usage = next(c for c in self.fake.calls if c[:3] == ["pane", "split", panes["lead"]])
        self.assertEqual(usage[usage.index("--direction") + 1], "down")
        self.assertGreater(float(usage[usage.index("--ratio") + 1]), 0.75)

    def test_rerun_resumes_each_team_member_without_replaying_the_lead_prompt(self):
        self.run_cli(INSIDE, setup="Kiro")
        panes = self.team_panes()["panes"]
        self.fake.start_agents()
        self.run_cli(INSIDE, cwd=ROOT / "src", setup="Codex")   # the menu is not asked on a reroute
        self.assertEqual(self.menu_calls, [])
        self.assertFalse([c for c in self.fake.calls if c[:2] == ["pane", "split"]])
        runs = self.fake.runs()
        self.assertEqual(set(runs), {panes[r] for r in ("lead", "usage", *herdr_ai.CREW)})
        for role in ("lead", *herdr_ai.CREW):
            native = self.resumed_argv(panes[role])
            self.assertEqual(native[:2], ["/vendor/kiro-cli", "chat"])
            self.assertEqual(native[-2:], ["--resume-id", "session-" + panes[role]])
            self.assertNotIn("old ready prompt", native)
            self.assertNotIn("--picker-return", runs[panes[role]][-1])

    def test_refresh_retains_working_blocked_unknown_and_unsupported_sessions(self):
        self.run_cli(INSIDE)
        panes = self.team_panes()["panes"]
        self.fake.start_agents()
        self.fake.panes[panes["codex"]]["agent_status"] = "working"
        self.fake.panes[panes["claude"]]["agent_status"] = "blocked"
        self.fake.panes[panes["agy"]].pop("agent_session")
        self.fake.panes[panes["cursor"]]["children"][0]["argv"].append("--unknown-option")
        self.run_cli(INSIDE)
        self.assertEqual(set(self.stopped), {panes["picker"], panes["usage"]})
        self.assertEqual(set(self.fake.runs()), set(self.stopped))

    def test_refresh_never_uses_a_different_providers_stale_session(self):
        self.run_cli(INSIDE)
        panes = self.team_panes()["panes"]
        self.fake.start_agents()
        self.fake.panes[panes["codex"]]["agent_session"]["agent"] = "claude"
        self.run_cli(INSIDE)
        self.assertNotIn(panes["codex"], self.stopped)

    def test_refresh_keeps_an_exited_agent_with_session_identity_at_its_shell(self):
        self.run_cli(INSIDE)
        panes = self.team_panes()["panes"]
        self.fake.start_agents()
        self.fake.panes[panes["codex"]]["argv"] = None
        self.run_cli(INSIDE)
        self.assertNotIn(panes["codex"], self.stopped)

    def test_refresh_leaves_unrelated_programs_in_agent_slots_running(self):
        self.run_cli(INSIDE)
        pane = self.team_panes()["panes"]["codex"]
        self.fake.start_agents()
        self.fake.panes[pane] = {"argv": ["npm", "run", "dev"]}
        self.run_cli(INSIDE)
        self.assertNotIn(pane, self.stopped)

    def test_refresh_of_a_quit_agent_reloads_its_selector_instead_of_a_blank_provider(self):
        self.run_cli(INSIDE)
        pane = self.team_panes()["panes"]["codex"]
        self.fake.start_agents()
        selector = [sys.executable, str(herdr_ai.ENTRY), "--picker-return"]
        self.fake.panes[pane]["argv"] = selector
        self.fake.panes[pane]["children"] = []
        self.run_cli(INSIDE)
        self.assertEqual(self.fake.runs()[pane][-1], self.joined(selector))

    def test_repeated_refreshes_keep_the_same_session_without_duplicate_resume_flags(self):
        self.run_cli(INSIDE)
        panes = self.team_panes()["panes"]
        for _ in range(3):
            self.fake.start_agents()
            self.run_cli(INSIDE)
            for role, flag in (("codex", "resume"), ("claude", "--resume"),
                               ("cursor", "--resume"), ("agy", "--conversation")):
                argv = self.resumed_argv(panes[role])
                self.assertEqual(argv.count(flag), 1)
                self.assertEqual(argv[-1], "session-" + panes[role])

    def test_refresh_rechecks_pane_identity_and_activity_before_stopping(self):
        import copy
        from unittest.mock import Mock
        pane = "w9:p2"
        initial = {"terminal_id": "original", "agent": "codex", "agent_status": "idle",
                   "agent_session": {"agent": "codex", "kind": "id", "value": "original-session"}}
        process = {"shell_pid": 10, "foreground_process_group_id": 20,
                   "foreground_processes": [{"pid": 20, "name": "codex", "argv": ["/vendor/codex", "-m", "sol"]}]}
        for field, changed in (("agent_status", "working"), ("terminal_id", "replacement"),
                               ("agent_session", {"agent": "codex", "kind": "id", "value": "new-session"})):
            updated = copy.deepcopy(initial)
            updated[field] = changed
            herdr = SimpleNamespace(tab_panes=Mock(side_effect=[{pane: initial}, {pane: updated}]),
                                    process_info=Mock(return_value=process))
            with self.subTest(field=field), patch.dict(os.environ, INSIDE), \
                 patch.object(herdr_ai, "stop_panes") as stop:
                self.assertEqual(herdr_ai.route(herdr, str(ROOT), {"codex": pane}, False,
                                               preserve_context=True), [])
                stop.assert_not_called()

    def test_agent_route_restarts_team_roles_with_their_commands(self):
        self.run_cli(INSIDE, setup="Claude")
        panes = self.team_panes()["panes"]
        self.fake.start_agents()
        self.run_cli(INSIDE, cwd=ROOT / "src", entry=herdr_ai.route_main)
        runs = self.fake.runs()
        self.assertIn("--picker-crew " + panes["lead"], runs[panes["crew1"]][1])
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
        with patch.object(herdr_ai.sys, "executable", "/opt/my python/bin/python3"):
            words = shlex.split(herdr_ai.picker_command(["--picker-launch", "Codex"]))
        self.assertEqual(words[0], "/opt/my python/bin/python3")
        self.assertEqual(words[-2:], ["--picker-launch", "Codex"])



class TeamPlanTests(unittest.TestCase):
    def test_team_plan_defaults_to_individual(self):
        import team
        with patch.object(team, "mode", return_value="individual"):
            self.assertEqual(herdr_ai.team_plan("Codex"), {"lead": "Codex", "gemini": False, "mode": "individual"})
        self.assertEqual(herdr_ai.team_plan("Codex", "individual")["mode"], "individual")
        self.assertNotIn("mode", herdr_ai.team_plan("Codex", "orchestrator"))
        self.assertEqual(herdr_ai.team_plan("Star", "individual"), {"lead": "Star", "gemini": False})

    def test_mode_setting_defaults_to_individual(self):
        import preferences
        import team
        with patch.object(preferences, "get", return_value=None):
            self.assertEqual(team.mode(), "individual")
        for value in ("off", "orchestrator"):
            with patch.object(preferences, "get", return_value=value):
                self.assertEqual(team.mode(), value)
        self.assertEqual(herdr_ai.team_plan("Codex", "off"), {"lead": "Codex", "gemini": False, "mode": "off"})

if __name__ == "__main__":
    unittest.main()
