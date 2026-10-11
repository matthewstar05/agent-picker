import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import session_refresh
import agent_picker


class SessionRefreshTests(unittest.TestCase):
    def test_stored_finds_directory_scoped_conversations(self):
        with tempfile.TemporaryDirectory() as home:
            claude = Path(home, ".claude", "projects", "-work-my-app")
            claude.mkdir(parents=True)
            (claude / "abc.jsonl").write_text("{}")
            import hashlib
            Path(home, ".cursor", "chats", hashlib.md5(b"/work/my.app").hexdigest(), "xyz").mkdir(parents=True)
            # Windows finds the home folder through USERPROFILE.
            with patch.dict(os.environ, {"HOME": home, "USERPROFILE": home}), patch.dict(os.environ, {}, clear=False):
                os.environ.pop("CLAUDE_CONFIG_DIR", None)
                self.assertTrue(session_refresh.stored("Claude", "abc", "/work/my.app"))
                self.assertFalse(session_refresh.stored("Claude", "abc", "/elsewhere"))
                self.assertFalse(session_refresh.stored("Claude", "never-prompted", "/work/my.app"))
                self.assertTrue(session_refresh.stored("Cursor", "xyz", "/work/my.app"))
                self.assertFalse(session_refresh.stored("Cursor", "xyz", "/elsewhere"))
                self.assertIsNone(session_refresh.stored("Codex", "abc", "/elsewhere"))

    def test_star_resume_requires_the_conversation_directory(self):
        with tempfile.TemporaryDirectory() as home:
            folder = Path(home) / "star" / "conversations"
            folder.mkdir(parents=True)
            (folder / "sid.json").write_text(json.dumps({"id": "sid", "cwd": "/work/app"}), encoding="utf-8")
            with patch.dict(os.environ, {"AGENT_PICKER_STATE_HOME": home}):
                self.assertTrue(session_refresh.stored("Star", "sid", "/work/app"))
                self.assertFalse(session_refresh.stored("Star", "sid", "/work/other"))
                self.assertFalse(session_refresh.stored("Star", "missing", "/work/app"))
                for corrupt in ([], {"id": "different", "cwd": "/work/app"},
                                {"id": "sid", "cwd": "/work/app", "sessions": []}):
                    (folder / "sid.json").write_text(json.dumps(corrupt))
                    self.assertFalse(session_refresh.stored("Star", "sid", "/work/app"))
                self.assertFalse(session_refresh.stored("Star", "../sid", "/work/app"))

    def test_star_local_identity_requires_exact_fresh_unambiguous_process_and_place(self):
        with tempfile.TemporaryDirectory() as home:
            folder = Path(home) / "star"
            (folder / "conversations").mkdir(parents=True)
            (folder / "mesh/members").mkdir(parents=True)
            (folder / "conversations/exact.json").write_text(json.dumps({"id": "exact", "cwd": "/work/app"}))
            member = {"pid": 17, "pane": "w1:p2", "tab": "w1:t1", "workspace": "w1",
                      "socket": "/test/socket", "conversation": "exact", "cwd": "/work/app",
                      "status": "idle", "updated": 100}
            path = folder / "mesh/members/lead-17.json"
            info = {"foreground_processes": [{"pid": 17, "argv": [sys.executable, "/src/starcli.py"]}]}
            env = {"AGENT_PICKER_STATE_HOME": home, "HERDR_SOCKET_PATH": "/test/socket",
                   "HERDR_TAB_ID": "w1:t1", "HERDR_WORKSPACE_ID": "w1"}
            with patch.dict(os.environ, env):
                def read(data):
                    path.write_text(json.dumps(data))
                    return session_refresh.star_identity(info, "w1:p2", now=105)
                result = read(member)
                self.assertEqual(result["agent_session"]["value"], "exact")
                for key, value in (("pid", 18), ("pane", "w1:p9"), ("socket", "/other/socket"),
                                   ("tab", "w1:t2"), ("workspace", "w2"), ("updated", 80),
                                   ("updated", 110), ("conversation", "missing"), ("status", "unknown"),
                                   ("cwd", "/other"), ("conversation", "../exact")):
                    with self.subTest(key=key, value=value):
                        self.assertIsNone(read({**member, key: value}))
                self.assertEqual(read({**member, "status": "working"})["agent_status"], "working")
                self.assertEqual(read({**member, "draft": True})["agent_status"], "working")
                self.assertEqual(read({**member, "ready": False})["agent_status"], "working")
                path.write_text(json.dumps(member))
                (path.parent / "duplicate.json").write_text(json.dumps(member))
                self.assertIsNone(session_refresh.star_identity(info, "w1:p2", now=105))

    def test_exact_session_selectors_for_every_workbench_provider(self):
        cases = [
            ("Claude", ["/vendor/claude", "--effort", "high", "--model", "opus", "--dangerously-skip-permissions"], "--resume"),
            ("Codex", ["/vendor/codex", "-m", "sol", "-c", "model_reasoning_effort=high", "--dangerously-bypass-approvals-and-sandbox"], "resume"),
            ("Cursor", ["/vendor/cursor-agent", "--model", "grok", "--yolo", "--trust"], "--resume"),
            ("Kiro", ["/vendor/kiro-cli", "chat", "--model", "opus", "--effort", "medium", "--trust-all-tools"], "--resume-id"),
            ("Antigravity", ["/vendor/agy", "--model", "gemini", "--dangerously-skip-permissions"], "--conversation"),
            ("Star", [sys.executable, "/src/starcli.py", "--name", "lead", "--role", "lead", "--plan"], "--resume-id"),
        ]
        for name, argv, flag in cases:
            with self.subTest(name=name):
                self.assertEqual(session_refresh.resume_argv(name, argv, "exact-pane-id"), argv + [flag, "exact-pane-id"])

    def test_claude_refresh_replaces_fork_and_old_session_without_replaying_prompt(self):
        argv = ["claude", "--resume", "old-id", "--fork-session", "--session-id=new-id", "--continue",
                "--model", "opus", "--append-system-prompt", "keep these instructions", "old task"]
        self.assertEqual(session_refresh.resume_argv("Claude", argv, "current-id"),
                         ["claude", "--model", "opus", "--append-system-prompt", "keep these instructions",
                          "--resume", "current-id"])

    def test_codex_refresh_replaces_old_resume_and_preserves_toml_team_brief(self):
        for command in ("resume", "fork"):
            argv = ["codex", command, "old-id", "-c", 'developer_instructions="resume this crew"',
                    "--last", "--all", "-m", "luna", "old task"]
            self.assertEqual(session_refresh.resume_argv("Codex", argv, "current-id"),
                             ["codex", "-c", 'developer_instructions="resume this crew"', "-m", "luna",
                              "resume", "current-id"])

    def test_team_opening_prompts_are_never_submitted_again(self):
        for name, argv, expected in [
            ("Kiro", ["kiro-cli", "chat", "--model", "opus", "Reply ready"], ["kiro-cli", "chat", "--model", "opus", "--resume-id", "sid"]),
            ("Cursor", ["cursor-agent", "--model=grok", "Reply ready"], ["cursor-agent", "--model=grok", "--resume", "sid"]),
            ("Antigravity", ["agy", "--model", "gemini", "-i", "Reply ready"], ["agy", "--model", "gemini", "--conversation", "sid"]),
        ]:
            with self.subTest(name=name):
                self.assertEqual(session_refresh.resume_argv(name, argv, "sid"), expected)

    def test_cursor_bun_entry_and_system_certificate_option_are_preserved(self):
        argv = ["/vendor/cursor-agent", "--use-system-ca", "/vendor/versions/current/index.js", "--yolo", "--model", "saved-model"]
        self.assertEqual(session_refresh.resume_argv("Cursor", argv, "sid"), argv + ["--resume", "sid"])

    def test_provider_options_are_not_confused_with_values_or_delimited_prompt(self):
        argv = ["claude", "--append-system-prompt", "--continue", "--", "old task", "--model", "old"]
        self.assertEqual(session_refresh.resume_argv("Claude", argv, "sid"),
                         ["claude", "--append-system-prompt", "--continue", "--resume", "sid"])

    def test_unknown_options_noninteractive_launches_and_empty_identity_are_refused(self):
        for name, argv, sid in [("Claude", ["claude", "--print", "old task"], "sid"),
                                ("Codex", ["codex", "--new-option", "value"], "sid"),
                                ("Codex", ["codex", "-m"], "sid"),
                                ("Codex", ["codex"], ""), ("Unknown", ["unknown"], "sid")]:
            with self.subTest(argv=argv), self.assertRaises(ValueError):
                session_refresh.resume_argv(name, argv, sid)

    def test_native_process_detection_excludes_picker_mcp_and_ambiguous_agents(self):
        native = {"pid": 7, "argv": [r"C:\Vendor\CODEX.EXE", "-m", "sol"], "name": "CODEX.EXE"}
        info = {"foreground_processes": [
            {"pid": 1, "argv": [sys.executable, "agent_picker.py", "--picker-launch", "Codex"]},
            {"pid": 2, "argv": ["node", "mcp-codex.js"]}, native]}
        self.assertIs(session_refresh.native_process(info, "Codex"), native)
        info["foreground_processes"].append(dict(native, pid=8))
        self.assertIsNone(session_refresh.native_process(info, "Codex"))

    def test_star_refresh_keeps_the_script_and_drops_a_one_shot_goal(self):
        argv = [sys.executable, "/src/starcli.py", "--resume-id", "old", "--name", "crew1", "--role", "crew",
                "--goal", "ship it", "-p", "old prompt"]
        self.assertEqual(session_refresh.resume_argv("Star", argv, "current"),
                         [sys.executable, "/src/starcli.py", "--name", "crew1", "--role", "crew",
                          "--resume-id", "current"])

    def test_star_process_is_the_script_not_the_picker_that_named_it(self):
        picker = {"pid": 20, "name": "python", "argv": [sys.executable, "/src/agent_picker.py",
                                                        "--picker-launch", "Star", "--", "--name", "lead"]}
        star = {"pid": 21, "name": "python", "argv0": "starcli.py",
                "argv": [sys.executable, "/src/starcli.py", "--name", "lead", "--role", "lead"]}
        info = {"foreground_processes": [picker, star]}
        self.assertIs(session_refresh.native_process(info, "Star"), star)
        self.assertIsNone(session_refresh.native_process({"foreground_processes": [picker]}, "Star"))

    def test_encoded_launch_is_one_shell_safe_word_with_lossless_unicode_and_quotes(self):
        argv = ["C:/Program Files/vendor.exe", "café", 'a "quoted" value', "$value", "line\nbreak", "a&b"]
        encoded = session_refresh.encode(argv)
        self.assertRegex(encoded, r"^[A-Za-z0-9_=-]+$")
        self.assertEqual(session_refresh.decode(encoded), argv)
        for value in ("not valid!", session_refresh.encode([]), session_refresh.encode({"bad": True}),
                      session_refresh.encode(["nul\0byte"])):
            with self.subTest(value=value), self.assertRaises(ValueError):
                session_refresh.decode(value)

    def test_picker_relaunch_keeps_return_behavior_and_exact_native_arguments(self):
        argv = ["/vendor/claude", "--model", "saved", "--resume", "sid"]
        for returning in (False, True):
            args = (["--picker-return"] if returning else []) + ["--picker-relaunch", session_refresh.encode(argv)]
            with self.subTest(returning=returning), patch.object(sys, "argv", ["agent", *args]), \
                 patch.object(agent_picker, "launch", return_value=7) as launch, \
                 patch.object(agent_picker, "launch_and_return", return_value=8) as launch_return:
                self.assertEqual(agent_picker.main(), 8 if returning else 7)
                (launch_return if returning else launch).assert_called_once_with(argv)
                (launch if returning else launch_return).assert_not_called()

    def test_real_picker_relaunch_executes_fake_provider_in_current_directory(self):
        with tempfile.TemporaryDirectory(prefix="resume test ") as folder:
            folder = Path(folder)
            script, output = folder / "fake provider.py", folder / "args.json"
            script.write_text("import json,os,sys\nfrom pathlib import Path\n"
                              "Path(sys.argv[1]).write_text(json.dumps({'args': sys.argv[2:], 'cwd': os.getcwd()}))\n"
                              "sys.exit(7)\n")
            argv = [sys.executable, str(script), str(output), "--resume", "specific-id", "space value", "café"]
            result = subprocess.run([sys.executable, str(ROOT / "src/agent_picker.py"), "--picker-relaunch",
                                     session_refresh.encode(argv)], cwd=folder, capture_output=True, text=True)
            self.assertEqual(result.returncode, 7, result.stderr)
            saved = json.loads(output.read_text())
            self.assertEqual(saved["args"], argv[3:])
            self.assertEqual(Path(saved["cwd"]).resolve(), folder.resolve())


if __name__ == "__main__":
    unittest.main()
