import contextlib
import io
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
import grokbot as cli
import grokbot_gateway as gateway
import grokbot_sessions as sessions
import agent_picker as ap
import usage_panel as panel

BOT = "f44b012d-e4ad-498f-bf2b-5365a6c25d86"
MACHINE = {"machineId": "local", "label": "test.local", "connected": True, "localToolPermission": "ask"}


class FakeGateway:
    def __init__(self):
        self.entries, self.sent, self.stopped, self.approved = [], [], False, []
        self.unknown = self.rejected = self.foreign = False

    def create(self, name, description):
        return {"id": BOT, "name": name}

    def agent(self, ident):
        return {"id": ident, "isRunningTurn": False, "isRunning": False, "isComposingMessage": False}

    def transcript(self, ident):
        return list(self.entries)

    def send(self, ident, prompt, nonce, machine=None):
        self.sent.append((ident, prompt, nonce, machine))
        if self.rejected:
            raise gateway.GrokError("Unauthorized", "rejected")
        self.entries += [{"kind": "message", "id": "root", "role": "user", "clientNonce": nonce, "requestId": "run"},
                         {"kind": "send-message", "id": "progress", "requestId": "run", "message": {"type": "text", "content": "Checking files."}},
                         {"kind": "send-message", "id": "answer", "requestId": "run", "message": {"type": "text", "content": "Verified.\n" + cli.marker(nonce)}}]
        if self.foreign:
            self.entries.append({"kind": "send-message", "id": "foreign", "requestId": "another-run",
                                 "message": {"type": "text", "content": "Unrelated response"}})
        if self.unknown:
            raise gateway.GrokError("Lost receipt", "unknown")
        return None  # Current native API returns {accepted: true}, no root ID.

    def interrupt(self, ident):
        self.stopped = True

    def approval(self, ident, entry, accept):
        self.approved.append((entry["id"], accept))

    def machines(self):
        return [MACHINE]


class GrokBotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.env = patch.dict(os.environ, AGENT_PICKER_STATE_HOME=str(self.root / "state"), HERDR_ENV="")
        self.env.start()
        self.addCleanup(self.env.stop)
        self.gateway = FakeGateway()

    def conversation(self, kind="json", session=None):
        return cli.Conversation(self.gateway, cli.Output(kind), self.root, MACHINE, session, timeout=1, poll=0)

    def test_native_turn_uses_local_machine_and_correlated_final_only(self):
        self.gateway.foreign = True
        c = self.conversation()
        with contextlib.redirect_stdout(io.StringIO()) as output:
            c.turn("Fix the tests")
        result = json.loads(output.getvalue())
        self.assertFalse(result["is_error"])
        self.assertEqual(result["result"], "Verified.")
        self.assertEqual(self.gateway.sent[0][3], "local")
        self.assertIn(str(self.root), self.gateway.sent[0][1])
        self.assertIn("Do not launch another AI CLI", self.gateway.sent[0][1])
        saved = sessions.load(BOT)
        self.assertIsNone(saved["pending"])
        self.assertEqual(saved["messages"][-1]["user"], "Fix the tests")

    def test_unknown_receipt_observes_exact_nonce_without_replay(self):
        self.gateway.unknown = True
        c = self.conversation()
        with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()):
            c.turn("Inspect this project")
        self.assertEqual(len(self.gateway.sent), 1)
        self.assertEqual(json.loads(output.getvalue())["result"], "Verified.")

    def test_headless_stdin_runs_without_a_terminal_or_app_window(self):
        with patch.object(cli, "Gateway", return_value=self.gateway), patch.object(cli, "select_machine", return_value=MACHINE), \
             patch.object(cli.time, "sleep"), patch.object(cli.sys, "stdin", io.StringIO("Review the project")), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            result = cli.main(["-p", "--output-format", "json", "--workspace", str(self.root)])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())["result"], "Verified.")
        self.assertEqual(len(self.gateway.sent), 1)
        self.assertIn("Review the project", self.gateway.sent[0][1])

    def test_delayed_approval_acknowledgement_does_not_prompt_twice(self):
        c = self.conversation()
        c.output.interactive = True
        send = self.gateway.send
        def approved_turn(*args, **kwargs):
            result = send(*args, **kwargs)
            self.gateway.entries.insert(1, {"id": "approval", "kind": "send-message", "requestId": "run",
                                         "message": {"type": "auto-review-approval", "approval":
                                                     {"requestId": "permission", "status": "pending", "command": "test"}}})
            return result
        with patch.object(self.gateway, "send", side_effect=approved_turn), patch("builtins.input", return_value="y") as answer, \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            c.turn("Task requiring approval")
        self.assertEqual(answer.call_count, 1)
        self.assertEqual(self.gateway.approved, [("approval", True)])

    def test_unknown_receipt_waits_for_nonce_before_displaying_unrelated_entries(self):
        c = self.conversation()
        c.timeout = 0.01
        self.gateway.send = lambda *args, **kwargs: self.gateway.entries.append(
            {"id": "foreign", "kind": "send-message", "message": {"type": "text", "content": "Another turn"}})
        with contextlib.redirect_stdout(io.StringIO()) as output, self.assertRaises(gateway.GrokError):
            c.turn("A new task")
        self.assertEqual(output.getvalue(), "")
        self.assertIsNotNone(sessions.load(BOT)["pending"])

    def test_rejected_submission_does_not_leave_a_false_pending_task(self):
        self.gateway.rejected = True
        c = self.conversation()
        with self.assertRaisesRegex(gateway.GrokError, "Unauthorized"):
            c.turn("Task")
        self.assertIsNone(sessions.load(BOT)["pending"])

    def test_crash_saved_pending_request_resumes_without_sending(self):
        c = self.conversation()
        c.ensure_session()
        nonce = gateway.request_id()
        c.session["pending"] = {"nonce": nonce, "text": "Original task", "root": None, "before": [], "shown": []}
        sessions.save(c.session)
        self.gateway.entries = [{"id": "u", "kind": "message", "role": "user", "clientNonce": nonce, "requestId": "original"},
                                {"id": "a", "kind": "send-message", "requestId": "original",
                                 "message": {"type": "text", "content": "Recovered " + cli.marker(nonce)}}]
        with contextlib.redirect_stdout(io.StringIO()) as output:
            c.turn()
        self.assertEqual(self.gateway.sent, [])
        self.assertEqual(json.loads(output.getvalue())["result"], "Recovered")

    def test_new_prompt_closes_an_abandoned_request_without_repeating_it(self):
        c = self.conversation()
        c.ensure_session()
        c.session["pending"] = {"nonce": "old", "text": "Do not repeat", "root": None, "before": [], "shown": []}
        sessions.save(c.session)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            c.turn("Another task")
        self.assertEqual(len(self.gateway.sent), 1)
        self.assertIn("Another task", self.gateway.sent[0][1])
        self.assertNotIn("Do not repeat", self.gateway.sent[0][1])
        saved = sessions.load(BOT)
        self.assertIsNone(saved["pending"])
        self.assertEqual([(m["user"], m.get("status")) for m in saved["messages"]],
                         [("Do not repeat", "interrupted"), ("Another task", None)])
        self.assertEqual(json.loads(output.getvalue())["result"], "Verified.")

    def test_new_prompt_first_records_a_reply_that_already_arrived(self):
        # The 2026-10-22 case: Esc's stop went unconfirmed, then the bot finished anyway.
        c = self.conversation()
        c.ensure_session()
        nonce = gateway.request_id()
        c.session["pending"] = {"nonce": nonce, "text": "test", "root": "u", "request_id": "original",
                                "before": [], "shown": []}
        sessions.save(c.session)
        self.gateway.entries = [{"id": "u", "kind": "message", "role": "user", "clientNonce": nonce, "requestId": "original"},
                                {"id": "a", "kind": "send-message", "requestId": "original",
                                 "message": {"type": "text", "content": "Test worked. " + cli.marker(nonce)}}]
        with contextlib.redirect_stdout(io.StringIO()) as output:
            c.turn("Next task")
        self.assertEqual(len(self.gateway.sent), 1)
        saved = sessions.load(BOT)
        self.assertEqual([(m["user"], m["assistant"]) for m in saved["messages"]],
                         [("test", "Test worked."), ("Next task", "Verified.")])
        results = [json.loads(line) for line in output.getvalue().splitlines() if line.strip()]
        self.assertEqual([r["result"] for r in results], ["Verified."])

    def test_first_message_waits_for_a_new_bots_greeting_turn(self):
        busy = iter([True, True, False])
        self.gateway.agent = lambda ident: {"id": ident, "isRunningTurn": next(busy, False), "isRunning": False}
        c = self.conversation()
        c.wait = lambda seconds: None
        with contextlib.redirect_stdout(io.StringIO()) as output:
            c.turn("First task")
        self.assertEqual(len(self.gateway.sent), 1)
        self.assertEqual(json.loads(output.getvalue())["result"], "Verified.")

    def test_a_bot_that_stays_busy_is_still_refused(self):
        self.gateway.agent = lambda ident: {"id": ident, "isRunningTurn": True, "isRunning": True}
        c = self.conversation()
        c.wait = lambda seconds: None
        with patch.object(cli.time, "monotonic", side_effect=[0, 0] + [100] * 10):
            with self.assertRaisesRegex(gateway.GrokError, "already working"):
                c.turn("Task")
        self.assertEqual(self.gateway.sent, [])

    def test_transcript_gap_never_reports_a_success(self):
        c = self.conversation()
        c.ensure_session()
        c.session["pending"] = {"nonce": "n", "text": "Task", "root": "missing-root", "before": ["old"], "shown": []}
        sessions.save(c.session)
        self.gateway.entries = [{"id": "foreign", "kind": "send-message", "message": {"content": "Done"}}]
        with self.assertRaisesRegex(gateway.GrokError, "coverage"):
            c.turn()
        self.assertIsNotNone(sessions.load(BOT)["pending"])

    def test_stop_requires_native_idle_confirmation(self):
        c = self.conversation()
        c.ensure_session()
        c.session["pending"] = {"nonce": "n", "text": "Long task"}
        sessions.save(c.session)
        with contextlib.redirect_stderr(io.StringIO()):
            c.stop()
        self.assertTrue(self.gateway.stopped)
        self.assertIsNone(sessions.load(BOT)["pending"])
        self.assertEqual(sessions.load(BOT)["messages"][-1]["status"], "interrupted")

    def test_headless_approval_is_not_accepted_automatically(self):
        c = self.conversation()
        card = {"status": "pending", "command": "touch file", "requestId": "r"}
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaisesRegex(gateway.GrokError, "Approval required"):
            c.approve({"id": "e"}, card)
        self.assertEqual(self.gateway.approved, [])

    def test_print_text_mode_writes_only_the_final_answer_like_claude(self):
        c = self.conversation("text")
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            c.turn("Fix the tests")
        self.assertEqual(out.getvalue(), "Verified.\n")
        self.assertEqual(err.getvalue(), "")
        self.gateway.entries.clear()
        c.output.verbose = True
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            c.turn("Again")
        self.assertEqual(out.getvalue(), "Verified.\n")
        self.assertIn("Checking files.", err.getvalue())

    def test_json_result_reports_duration_and_turns(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.conversation().turn("Fix the tests")
        result = json.loads(output.getvalue())
        self.assertIsInstance(result["duration_ms"], int)
        self.assertEqual(result["num_turns"], 1)

    def test_appended_system_prompt_is_saved_and_sent_every_turn(self):
        c = self.conversation()
        c.system = "Answer in one line."
        with contextlib.redirect_stdout(io.StringIO()):
            c.turn("First")
        self.assertEqual(sessions.load(BOT)["system"], "Answer in one line.")
        resumed = cli.Conversation(self.gateway, cli.Output("json"), self.root, MACHINE, sessions.load(BOT), timeout=1, poll=0)
        self.gateway.entries.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            resumed.turn("Second")
        for sent in self.gateway.sent:
            self.assertIn("Standing instructions for this session", sent[1])
            self.assertIn("Answer in one line.", sent[1])
        self.assertLess(self.gateway.sent[1][1].index("Answer in one line."), self.gateway.sent[1][1].index("Second"))

    def test_piped_stdin_joins_a_prompt_argument(self):
        with patch.object(cli, "Gateway", return_value=self.gateway), patch.object(cli, "select_machine", return_value=MACHINE), \
             patch.object(cli.time, "sleep"), patch.object(cli.sys, "stdin", io.StringIO("def broken(): pass")), \
             contextlib.redirect_stdout(io.StringIO()):
            result = cli.main(["-p", "explain this file", "-C", str(self.root)])
        self.assertEqual(result, 0)
        self.assertIn("explain this file\n\ndef broken(): pass", self.gateway.sent[0][1])

    def test_always_allows_the_same_action_for_the_rest_of_the_run(self):
        c = self.conversation()
        c.output.interactive = True
        c.ensure_session()
        entries = [{"id": i, "kind": "send-message", "message": {"type": "local-tool-permission",
                    "ask": {"status": "pending", "requestId": i}}} for i in ("e1", "e2", "e3")]
        self.gateway.entries += entries
        same = {"status": "pending", "command": "npm test", "workingDirectory": "/p", "requestId": "r"}
        with patch("builtins.input", side_effect=["a", "n"]) as answer, contextlib.redirect_stderr(io.StringIO()):
            c.approve(entries[0], same)
            c.approve(entries[1], dict(same, reason="again"))
            c.approve(entries[2], dict(same, command="rm -rf build"))
        self.assertEqual(answer.call_count, 2)
        self.assertEqual(self.gateway.approved, [("e1", True), ("e2", True), ("e3", False)])

    def test_claude_and_codex_style_aliases(self):
        args = cli.parser().parse_args(["-r", "abc", "-C", "/tmp", "--verbose", "--append-system-prompt", "Be brief"])
        self.assertEqual((args.resume, args.workspace, args.verbose, args.system), ("abc", Path("/tmp"), True, "Be brief"))
        self.assertEqual(cli.parser().parse_args(["--cd", "/tmp"]).workspace, Path("/tmp"))
        self.assertEqual(cli.parser().parse_args(["--workspace", "/tmp"]).workspace, Path("/tmp"))
        self.assertIn("clear", [name for name, _, _ in cli.COMMANDS])

    def test_saved_session_is_private_and_has_one_writer(self):
        c = self.conversation()
        c.ensure_session()
        path = sessions.directory() / (BOT + ".json")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        with sessions.lease(c.session):
            with self.assertRaisesRegex(gateway.GrokError, "another CLI"):
                with sessions.lease(c.session):
                    self.fail("second writer acquired the conversation")

    def test_wrong_workspace_and_path_traversal_are_rejected(self):
        c = self.conversation()
        c.ensure_session()
        c.cwd = str(self.root / "other-project")
        with self.assertRaisesRegex(gateway.GrokError, "belongs to"):
            c.ensure_session()
        with self.assertRaises(gateway.GrokError):
            sessions.save({"id": "../../arbitrary"})

    def test_machine_selection_never_silently_chooses_another_host(self):
        with patch.object(cli.socket, "gethostname", return_value="test.local"), patch.object(cli.socket, "getfqdn", return_value="test.local"):
            self.assertEqual(cli.select_machine([MACHINE])["machineId"], "local")
            with self.assertRaises(gateway.GrokError):
                cli.select_machine([dict(MACHINE, label="someone-else.local")])
            with self.assertRaises(gateway.GrokError):
                cli.select_machine([dict(MACHINE, connected=False)])
            with self.assertRaisesRegex(gateway.GrokError, "disabled"):
                cli.select_machine([dict(MACHINE, localToolPermission="never")])

    def test_untrusted_urls_cannot_receive_credentials(self):
        for url in ("https://cursor.sh.evil.test", "http://api2.cursor.sh", "https://user:password@api2.cursor.sh",
                    "https://api2.cursor.sh/?token=secret", "https://api2.cursor.sh:8443"):
            with self.subTest(url=url), self.assertRaises(gateway.GrokError):
                gateway.allowed_url(url)
        self.assertEqual(gateway.allowed_url("https://box.cursorvm.com", True), "https://box.cursorvm.com")
        self.assertIsNone(gateway.NoRedirect().redirect_request(None, None, None, None, None, None))

    def test_gateway_uses_native_acceptance_and_machine_routing(self):
        calls = []
        def request(url, body, headers):
            calls.append((url, body, headers))
            if url.endswith("EnsureSandBox"):
                return {"gatewayUrl": "https://box.cursorvm.com", "gatewayToken": "private", "networkToken": "network"}
            return {"accepted": True}
        g = gateway.Gateway(token="account", request=request)
        self.assertIsNone(g.send(BOT, "task", "nonce", machine="local"))
        self.assertEqual(calls[-1][1], {"agentId": BOT, "prompt": "task", "clientNonce": "nonce", "machineId": "local"})
        self.assertEqual(calls[-1][2]["x-anyrun-network-token"], "network")

    def test_malformed_transcript_is_reported_as_a_gateway_error(self):
        g = gateway.Gateway()
        with patch.object(g, "call", return_value={"entries": [None, "invalid"]}):
            with self.assertRaisesRegex(gateway.GrokError, "Invalid Grok Bot transcript"):
                g.transcript(BOT)

    def test_terminal_control_sequences_are_removed(self):
        self.assertEqual(cli.clean("hello\x1b]52;c;BAD\x07\x1b[31mworld\x00"), "helloworld")

    def test_fake_picker_launch_passes_prompt_and_workspace_unchanged(self):
        exe = self.root / "fake-grokbot"
        output = self.root / "argv.json"
        exe.write_text("#!/usr/bin/env python3\nimport json,os,sys\nfrom pathlib import Path\n"
                       + "Path(" + repr(str(output)) + ").write_text(json.dumps({'argv':sys.argv[1:],'cwd':os.getcwd()}))\n")
        exe.chmod(0o755)
        config = self.root / "config.json"
        config.write_text(json.dumps({"agents": {"Grok Bot": {"command": str(exe)}}}))
        env = dict(os.environ, AGENT_PICKER_CONFIG=str(config))
        run = subprocess.run([sys.executable, str(ROOT / "src/agent_picker.py"), "--picker-launch", "Grok Bot", "-p", "quoted task"],
                             cwd=self.root, env=env, capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(output.read_text()), {"argv": ["-p", "quoted task"], "cwd": str(self.root)})


class GrokBotUsageTests(unittest.TestCase):
    def test_permanent_bar_has_an_unavailable_placeholder_and_settings_toggle(self):
        import preferences
        with tempfile.TemporaryDirectory() as folder, patch.object(preferences, "PATH", Path(folder) / "p.json"), patch.object(preferences, "_values", {}):
            reading = {"windows": [{"label": "cr", "pct": 10}, {"label": "ex", "pct": 20}]}
            self.assertEqual(preferences.get(panel.CURSOR_GROK_TRANSIENT.key), False)
            windows = ap.display_windows("Cursor", reading)
            self.assertEqual(windows[-1], {"label": "gb", "pct": None, "resets": None})
            preferences.cycle(panel.CURSOR_GROK_TRANSIENT.key)
            self.assertEqual([w["label"] for w in ap.display_windows("Cursor", reading)], ["cr", "ex"])
            preferences.cycle(panel.CURSOR_GROK_TRANSIENT.key)
            self.assertEqual(ap.display_windows("Cursor", None)[0]["label"], "gb")

    def test_grok_pool_does_not_change_star_cursor_capacity(self):
        import star_meter
        now = 1800000000
        spec = {"Cursor": {"pools": True, "capacity": 1, "tiers": {"light": {"pool": "cr"}, "heavy": {"pool": "ex"}}}}
        reading = {"fetched": now, "windows": [{"label": "cr", "pct": 20}, {"label": "ex", "pct": 40}]}
        with patch.object(star_meter, "config", return_value=spec):
            before = star_meter.summary({"Cursor": reading}, {"Cursor"}, {"Cursor": {}}, now)
            reading["windows"].append({"label": "gb", "pct": 100})
            self.assertEqual(star_meter.summary({"Cursor": reading}, {"Cursor"}, {"Cursor": {}}, now), before)

    def test_cursor_style_input_keeps_shared_completion_and_multiline_rendering(self):
        from grokbot_ui import CursorInput
        editor = CursorInput(cli.COMMANDS)
        editor.drawn_rows = editor.cursor_row = 0
        editor.hint = lambda: ("Grok Bot · Auto", "Local · ask")
        editor.notice = None
        for text in ("", "/us", "read @src/", "multiline\nnext line"):
            editor.text, editor.pos, editor.sel = text, len(text), 0
            with patch("grokbot_ui.shutil.get_terminal_size", return_value=os.terminal_size((60, 20))), contextlib.redirect_stdout(io.StringIO()) as out:
                editor.draw()
            rendered = out.getvalue()
            self.assertIn("▄", rendered)
            self.assertIn("▀", rendered)
            self.assertIn("→", rendered)
            if text == "/us":
                self.assertIn("/usage", rendered)

    def test_cursor_three_pools_have_independent_reset_windows(self):
        def response(url, headers, body):
            if url.endswith("GetCurrentPeriodUsage"):
                return {"planUsage": {"autoPercentUsed": 10, "apiPercentUsed": 25}, "billingCycleEnd": 1800000000000}
            return {"usagePercent": 40, "nextResetTimestampUtc": "2027-01-01T00:00:00Z"}
        with patch.object(ap, "_keychain", return_value="private"), patch.object(ap, "_http_json", side_effect=response):
            windows = ap.usage_cursor()
        self.assertEqual([w["label"] for w in windows], ["cr", "ex", "gb"])
        self.assertEqual([w["pct"] for w in windows], [10, 25, 40])
        self.assertNotEqual(windows[-1]["resets"], windows[0]["resets"])
        self.assertEqual(panel.window_len("gb"), 7 * 86400)

    def test_missing_grok_usage_is_not_fabricated_or_allowed_to_break_cursor(self):
        with patch.object(ap, "_keychain", return_value="private"), patch.object(ap, "_http_json", side_effect=[
            {"planUsage": {"autoPercentUsed": 10}, "billingCycleEnd": 1800000000000}, OSError("offline")]):
            self.assertEqual([w["label"] for w in ap.usage_cursor()], ["cr"])
        with patch.object(ap, "_keychain", return_value="private"), patch.object(ap, "_http_json", side_effect=[OSError("offline"),
            {"usagePercent": 0, "nextResetTimestampUtc": "2027-01-01T00:00:00Z"}]):
            self.assertEqual(ap.usage_cursor()[0]["label"], "gb")

    def test_cursor_card_full_and_compact_retain_the_third_pool(self):
        import preferences
        now = 1800000000
        cursor = next(a for a in ap.AGENTS if a["name"] == "Cursor")
        reading = {"windows": [{"label": k, "pct": n, "resets": now + 3600} for k, n in (("cr", 11), ("ex", 22), ("gb", 33))], "fetched": now}
        with tempfile.TemporaryDirectory() as folder, patch.object(preferences, "PATH", Path(folder) / "p.json"), patch.object(preferences, "_values", {}), \
             patch.dict(panel.USAGE, {"Cursor": reading}, clear=True), patch.dict(panel.PRESENT, {"Cursor": 1}, clear=True):
            view = panel.View([cursor], pinned=True)
            for theme in panel.THEMES:
                preferences.put(panel.THEME.key, theme["name"])
                for width, height in ((100, 10), (55, 6), (85, 3)):
                    lines = panel.render(view, width, height, 1.5)
                    plain = [ap._ANSI.sub("", "".join(line) if isinstance(line, tuple) else line) for line in lines]
                    self.assertTrue(all(len(line) <= width for line in plain), (width, height, plain))
                    self.assertLessEqual(len(plain), height)
                    self.assertIn("gb", "\n".join(plain), (width, height, plain))
                    self.assertIn("67%", "\n".join(plain))

    def test_grokbot_process_counts_toward_cursor_usage_card(self):
        self.assertEqual(panel.provider_of("grokbot"), "Cursor")
        self.assertEqual(panel.from_argv(["python3", str(ROOT / "src/grokbot.py")]), "Cursor")


if __name__ == "__main__":
    unittest.main()
