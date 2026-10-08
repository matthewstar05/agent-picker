import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import agent_picker as picker
import platform_runtime as runtime
import settings


class PickerTests(unittest.TestCase):
    def test_original_launch_settings_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict(os.environ, AGENT_PICKER_CONFIG=str(Path(folder) / "config.json")):
                agents = {a["name"]: a for a in settings.load_agents()}
        self.assertEqual(agents["Codex"]["argv"][1:], ["-m", "gpt-6.1-sol", "-c", "model_reasoning_effort=high", "--dangerously-bypass-approvals-and-sandbox", "-c", "features.fast_mode=true", "-c", "service_tier=default"])
        self.assertIn("--dangerously-skip-permissions", agents["Claude"]["argv"])
        self.assertIn("--trust-all-tools", agents["Kiro"]["argv"])
        self.assertEqual(list(agents)[:5], ["Claude", "Codex", "Antigravity", "Cursor", "Kiro"])
        self.assertIn("OpenCode", agents)

    def test_local_platform_override(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "config.json"
            config.write_text(json.dumps({"agents": {"Cursor": {"argv": ["custom-cursor", "--custom"]}}}))
            with patch.dict(os.environ, {"AGENT_PICKER_CONFIG": str(config)}), patch.object(settings, "WINDOWS", True):
                agents = settings.load_agents()
                by_name = {a["name"]: a for a in agents}
                self.assertEqual(by_name["Cursor"]["argv"], ["custom-cursor", "--custom"])
                self.assertEqual(by_name["Codex"]["argv"][0], "codex")

    def test_config_rejects_recursive_provider(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "config.json"
            config.write_text(json.dumps({"agents": {"Cursor": {"argv": ["agent"]}}}))
            with patch.dict(os.environ, {"AGENT_PICKER_CONFIG": str(config)}):
                with self.assertRaisesRegex(ValueError, "recursion"):
                    settings.load_agents()

    def test_local_command_inherits_shared_arguments(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "config.json"
            config.write_text(json.dumps({"agents": {"Cursor": {"command": "vendor-cursor"}}}))
            with patch.dict(os.environ, {"AGENT_PICKER_CONFIG": str(config)}):
                agents = settings.load_agents()
            original = next(a for a in json.loads((ROOT / "agents.json").read_text()) if a["name"] == "Cursor")
            cursor = next(a for a in agents if a["name"] == "Cursor")
            self.assertEqual(cursor["argv"], ["vendor-cursor"] + original["argv"][1:])
            self.assertEqual(cursor["executables"], [])

    def test_resolution_uses_cursor_fallback_without_generic_agent(self):
        seen = []
        def which(command, path=None):
            seen.append(command)
            return "/vendor/cursor-agent" if command == "cursor-agent" else None
        with patch.object(runtime.shutil, "which", which):
            argv = runtime.resolve_argv({"argv": ["cursor-cli", "--model", "test"], "executables": ["cursor-agent"]})
        self.assertEqual(argv, ["/vendor/cursor-agent", "--model", "test"])
        self.assertNotIn("agent", seen)

    def test_render_fits_resized_terminals(self):
        with patch.object(picker, "USAGE", {}), patch.object(picker, "PENDING", set()):
            for cols in (1, 8, 17, 22, 24, 40, 60, 80, 120):
                for rows in (1, 5, 7, 10, 14, 24, 40):
                    for selected in range(5):
                        lines = picker.render(selected, [True] * 5, 0.3, cols, rows, agents=picker.AGENTS[:5])
                        self.assertLessEqual(len(lines), rows)
                        for line in lines:
                            self.assertLessEqual(picker.visible_len(line), cols, (cols, rows, selected))

    def test_catalog_profiles_are_complete(self):
        for agent in settings.load_agents():
            for key in ("name", "color", "pastel", "gradient", "short", "detail", "argv", "detect"):
                self.assertIn(key, agent, agent["name"])
            self.assertTrue(agent["detect"])

    def test_picker_lists_only_installed_agents(self):
        listed = [a for a in picker.AGENTS if a["name"] in ("Codex", "Kiro")]
        lines = picker.render(0, [True, True], 0.3, 80, 24, agents=listed)
        text = "".join(picker._ANSI.sub("", line) for line in lines)
        self.assertIn("Codex", text)
        self.assertIn("Kiro", text)
        self.assertNotIn("Claude", text)
        self.assertNotIn("OpenCode", text)

    def test_hidden_agents_leave_the_selector_until_shown_again(self):
        found = [a for a in picker.AGENTS if a["name"] in ("Codex", "Kiro", "OpenCode")]
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(picker.preferences, "PATH", Path(folder) / "preferences.json"), \
                patch.object(picker.preferences, "_values", None):
            self.assertEqual([a["name"] for a in picker.shown(found)], ["Codex", "Kiro", "OpenCode"])
            key = picker.shown_setting(found[2]).key
            self.assertIn(key, [s.key for s in picker.preferences.SETTINGS])
            picker.preferences.cycle(key)
            self.assertEqual([a["name"] for a in picker.shown(found)], ["Codex", "Kiro"])
            for agent in found[:2]:
                picker.preferences.put(picker.shown_setting(agent).key, False)
            self.assertEqual(len(picker.shown(found)), 3)   # never an empty selector
            picker.preferences._values = None   # saved choices survive a restart
            self.assertFalse(picker.preferences.get(key))

    def test_missing_providers_report_clear_error(self):
        with patch.object(picker, "installed", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "No supported CLI"):
                picker.choose()

    def test_native_launch_preserves_args_cwd_and_exit_status(self):
        with tempfile.TemporaryDirectory(prefix="picker test ") as folder:
            folder = Path(folder)
            script = folder / "fake cli.py"
            output = folder / "args.json"
            script.write_text("import json, os, sys\nfrom pathlib import Path\nPath(sys.argv[1]).write_text(json.dumps({'args': sys.argv[2:], 'cwd': os.getcwd()}))\nsys.exit(7)\n")
            config = folder / "config.json"
            config.write_text(json.dumps({"agents": {"Claude": {"argv": [sys.executable, str(script), str(output)]}}}))
            args = ["hello world", 'a "quoted" value', "unicode café", "--literal", "a&b", "$value"]
            env = dict(os.environ, AGENT_PICKER_CONFIG=str(config), AGENT_PICKER_STATE_HOME=str(folder / "state"), AGENT_PICKER_NO_PRETRUST="1")
            result = subprocess.run([sys.executable, str(ROOT / "src/agent_picker.py"), "--picker-launch", "Claude"] + args, cwd=folder, env=env, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 7, result.stderr)
            data = json.loads(output.read_text())
            self.assertEqual(data["args"], args)
            self.assertEqual(Path(data["cwd"]).resolve(), folder.resolve())
            self.assertEqual((folder / "state/last").read_text(), "Claude")

    @unittest.skipUnless(os.name == "nt", "Windows batch shims")
    def test_windows_batch_provider(self):
        with tempfile.TemporaryDirectory(prefix="picker batch ") as folder:
            folder = Path(folder)
            script = folder / "record.py"
            output = folder / "out.json"
            script.write_text("import json,sys\nfrom pathlib import Path\nPath(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))\nsys.exit(9)\n")
            shim = folder / "provider.cmd"
            shim.write_text(f'@echo off\n"{sys.executable}" "{script}" %*\nexit /b %errorlevel%\n')
            args = [str(output), "space value", "café", "--flag"]
            status = runtime.launch([str(shim)] + args)
            self.assertEqual(status, 9)
            self.assertEqual(json.loads(output.read_text()), args[1:])

    @unittest.skipUnless(os.name == "nt", "Windows keyboard")
    def test_windows_arrow_and_escape_keys(self):
        import msvcrt
        terminal = runtime.Terminal()
        for chars, expected in ((["\xe0", "H"], "up"), (["\x00", "P"], "down"), (["\x1b"], "esc"), (["\r"], "\r")):
            with patch.object(msvcrt, "getwch", side_effect=chars):
                self.assertEqual(terminal.read_key(), expected)

    def test_trust_files_preserve_existing_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            (home / ".claude.json").write_text(json.dumps({"keep": True, "projects": {"existing": {"other": 1}}}))
            codex = home / ".codex"
            codex.mkdir()
            (codex / "config.toml").write_text('model = "keep"\n')
            workspace = 'C:\\Users\\Example\\project "quoted"'
            with patch.object(picker.Path, "home", return_value=home), patch.dict(os.environ, {"CODEX_HOME": str(codex)}):
                picker.trust_claude([workspace])
                picker.trust_codex([workspace])
                picker.trust_codex([workspace])
            claude = json.loads((home / ".claude.json").read_text())
            self.assertTrue(claude["keep"])
            self.assertEqual(claude["projects"]["existing"]["other"], 1)
            text = (codex / "config.toml").read_text()
            self.assertTrue(text.startswith('model = "keep"'))
            self.assertEqual(text.count('trust_level = "trusted"'), 1)

    def test_claude_file_credentials_are_read_only(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".credentials.json"
            original = json.dumps({"claudeAiOauth": {"accessToken": "fake-test-token", "expiresAt": 9999999999999}})
            path.write_text(original)
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": folder}), patch.object(picker, "_http_json", return_value={"five_hour": {"utilization": 20, "resets_at": "2030-01-01T00:00:00Z"}}):
                windows = picker.usage_claude()
            self.assertEqual(windows[0]["pct"], 20)
            self.assertEqual(path.read_text(), original)

    def test_windows_cursor_and_antigravity_logins_are_read_only(self):
        usage = {"billingCycleEnd": "1900000000000", "planUsage": {"autoPercentUsed": 24, "apiPercentUsed": 5}}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "Cursor/auth.json"
            path.parent.mkdir()
            original = json.dumps({"accessToken": "fake-test-token", "refreshToken": "fake"})
            path.write_text(original)
            with patch.dict(os.environ, {"APPDATA": folder}), patch.object(picker, "WINDOWS", True), \
                    patch.object(picker, "_keychain", return_value=None), \
                    patch.object(picker, "_http_json", return_value=usage) as http:
                windows = picker.usage_cursor()
            self.assertEqual([(w["label"], w["pct"]) for w in windows], [("cr", 24.0), ("ex", 5.0)])
            self.assertIn("Bearer fake-test-token", http.call_args[0][1]["Authorization"])
            self.assertEqual(path.read_text(), original)
        token = {"token": {"access_token": "fake", "expiry": "2999-01-01T00:00:00Z"}}
        quota = {"groups": [{"displayName": "Gemini", "buckets": [
            {"window": "weekly", "remainingFraction": 0.75, "resetTime": "2999-01-01T00:00:00Z"}]}]}
        with patch.object(picker, "_keychain", return_value=None), \
                patch.object(picker, "_wincred", return_value=json.dumps(token)) as cred, \
                patch.object(picker, "_http_json", return_value=quota):
            self.assertEqual(picker.usage_antigravity()[0]["pct"], 25)
        cred.assert_called_once_with("gemini:antigravity")

    @unittest.skipIf(os.name == "nt", "macOS shell installer")
    def test_macos_install_is_idempotent_and_uses_live_checkout(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            rc = home / ".zshrc"
            rc.write_text('export KEEP=1\nalias agent="agent-picker"\n# Kiro CLI post block. Keep at the bottom of this file.\n# keep-bottom\n')
            env = dict(os.environ, HOME=folder, AGENT_PICKER_STATE_HOME=str(home / "state"))
            for _ in range(2):
                result = subprocess.run([sys.executable, str(ROOT / "scripts/install-macos.py")], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            text = rc.read_text()
            self.assertEqual(text.count("# >>> agent-picker managed >>>"), 1)
            self.assertIn("export KEEP=1", text)
            self.assertTrue(text.endswith("# keep-bottom\n"))
            wrapper = home / ".local/bin/agent-picker"
            self.assertIn(str(ROOT / "src/agent_picker.py"), wrapper.read_text())
            result = subprocess.run([str(wrapper), "--picker-doctor"], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_update_refuses_dirty_checkout(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".git").mkdir()
            with patch.object(settings, "ROOT", root), patch("subprocess.check_output", return_value=" M agents.json\n"), patch("subprocess.run") as run:
                with self.assertRaisesRegex(RuntimeError, "local changes"):
                    settings.update_checkout()
                run.assert_not_called()

    @unittest.skipUnless(shutil.which("git"), "Git required")
    def test_push_then_update_other_checkout(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            remote, author, consumer = (root / name for name in ("remote.git", "author", "consumer"))
            def git(*args):
                return subprocess.run(["git"] + list(map(str, args)), check=True, capture_output=True, text=True)
            git("init", "--bare", remote)
            git("init", "-b", "main", author)
            git("-C", author, "config", "user.name", "Test")
            git("-C", author, "config", "user.email", "test@example.invalid")
            git("-C", author, "remote", "add", "origin", remote)
            (author / "shared.json").write_text('{"model":"first"}\n')
            git("-C", author, "add", ".")
            git("-C", author, "commit", "-m", "Initial")
            git("-C", author, "push", "-u", "origin", "main")
            git("clone", "--branch", "main", remote, consumer)
            (author / "shared.json").write_text('{"model":"updated"}\n')
            git("-C", author, "commit", "-am", "Update")
            git("-C", author, "push")
            with patch.object(settings, "ROOT", consumer):
                settings.update_checkout()
            self.assertEqual((consumer / "shared.json").read_text(), (author / "shared.json").read_text())

    @unittest.skipIf(os.name == "nt", "POSIX pseudo-terminal")
    def test_interactive_menu_restores_terminal_after_resize_and_quit(self):
        import fcntl
        import pty
        import select
        import struct
        import termios
        import time
        master, slave = pty.openpty()
        old = termios.tcgetattr(slave)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        code = "import sys;sys.path.insert(0,sys.argv[1]);import agent_picker as a;a.installed=lambda x:True;a.FETCHERS.clear();a.main()"
        process = subprocess.Popen([sys.executable, "-c", code, str(ROOT / "src")], stdin=slave, stdout=slave, stderr=slave)
        data = b""
        start = time.monotonic()
        moved = quit_sent = False
        try:
            while time.monotonic() - start < 5:
                elapsed = time.monotonic() - start
                if elapsed > 0.25 and not moved:
                    os.write(master, b"\x1b[B")
                    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 10, 40, 0, 0))
                    moved = True
                if elapsed > 0.6 and not quit_sent:
                    os.write(master, b"q")
                    quit_sent = True
                if select.select([master], [], [], 0.01)[0]:
                    data += os.read(master, 65536)
                if process.poll() is not None:
                    while select.select([master], [], [], 0.02)[0]:
                        data += os.read(master, 65536)
                    break
            self.assertEqual(process.poll(), 0, data[-500:])
            new = termios.tcgetattr(slave)
            # macOS may add the kernel's PENDIN flag after canonical input is restored.
            pending = getattr(termios, "PENDIN", 0)
            old[3] &= ~pending
            new[3] &= ~pending
            self.assertEqual(new, old)
            self.assertIn(b"\x1b[?1049h", data)
            self.assertIn(b"\x1b[?1049l", data)
            self.assertIn(b"\x1b[?25h", data)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)
            os.close(slave)


if __name__ == "__main__":
    unittest.main()
