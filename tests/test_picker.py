import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import agent_picker as picker
import platform_runtime as runtime
import settings


class PickerTests(unittest.TestCase):
    def test_kiro_credits_follow_the_bar_direction(self):
        window = {"label": "mo", "pct": 0.2, "resets": time.time() + 86400, "used": 2.4, "limit": 1000.0}
        with patch.object(picker, "depleting", return_value=True):
            self.assertEqual(picker.credits_text(window), "997.60/1000")
        with patch.object(picker, "depleting", return_value=False):
            self.assertEqual(picker.credits_text(window), "2.40/1000 used")
        self.assertIsNone(picker.credits_text({"label": "5h", "pct": 10}))

    def test_original_launch_settings_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict(os.environ, AGENT_PICKER_CONFIG=str(Path(folder) / "config.json")):
                agents = {a["name"]: a for a in settings.load_agents()}
        self.assertEqual(agents["Codex"]["argv"][1:], ["-m", "gpt-6.1-sol", "-c", "model_reasoning_effort=high", "--dangerously-bypass-approvals-and-sandbox", "-c", "features.fast_mode=true", "-c", "service_tier=default"])
        self.assertIn("--dangerously-skip-permissions", agents["Claude"]["argv"])
        self.assertIn("--trust-all-tools", agents["Kiro"]["argv"])
        self.assertEqual(list(agents)[:7], ["Star", "Claude", "Codex", "Antigravity", "Cursor", "Grok Bot", "Kiro"])
        self.assertIn("OpenCode", agents)

    def test_local_argv_override(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "config.json"
            config.write_text(json.dumps({"agents": {"Cursor": {"argv": ["custom-cursor", "--custom"]}}}))
            with patch.dict(os.environ, {"AGENT_PICKER_CONFIG": str(config)}):
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
            self.assertIn(key, [s.key for s in picker.VISIBILITY.children])
            picker.preferences.cycle(key)
            self.assertEqual([a["name"] for a in picker.shown(found)], ["Codex", "Kiro"])
            for agent in found[:2]:
                picker.preferences.put(picker.shown_setting(agent).key, False)
            self.assertEqual(len(picker.shown(found)), 3)   # never an empty selector
            picker.preferences._values = None   # saved choices survive a restart
            self.assertFalse(picker.preferences.get(key))

    def test_star_starts_hidden_until_shown(self):
        star = [a for a in picker.AGENTS if a["name"] == "Star"]
        others = [a for a in picker.AGENTS if a["name"] == "Codex"]
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(picker.preferences, "PATH", Path(folder) / "preferences.json"), \
                patch.object(picker.preferences, "_values", None):
            self.assertEqual([a["name"] for a in picker.shown(star + others)], ["Codex"])
            picker.preferences.put(picker.shown_setting(star[0]).key, True)
            self.assertEqual([a["name"] for a in picker.shown(star + others)], ["Star", "Codex"])

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

    def test_return_mode_reopens_selector_after_agent_quits(self):
        import fcntl, pty, select, struct, termios
        with tempfile.TemporaryDirectory(prefix="picker test ") as folder:
            folder = Path(folder)
            marker = folder / "ran"
            script = folder / "fake cli.py"
            script.write_text("import sys\nfrom pathlib import Path\nPath(sys.argv[1]).write_text('yes')\n")
            config = folder / "config.json"
            config.write_text(json.dumps({"agents": {"Claude": {"argv": [sys.executable, str(script), str(marker)]}}}))
            env = dict(os.environ, AGENT_PICKER_CONFIG=str(config), AGENT_PICKER_STATE_HOME=str(folder / "state"),
                       AGENT_PICKER_NO_PRETRUST="1", TERM="xterm-256color")
            pid, fd = pty.fork()
            if pid == 0:
                os.chdir(folder)
                os.execve(sys.executable, [sys.executable, str(ROOT / "src/agent_picker.py"),
                                           "--picker-return", "--picker-launch", "Claude"], env)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
            screen, deadline = b"", time.monotonic() + 15
            try:
                while b"launch" not in screen and time.monotonic() < deadline:
                    if select.select([fd], [], [], 0.2)[0]:
                        screen += os.read(fd, 65536)
                self.assertEqual(marker.read_text(), "yes")
                self.assertIn(b"launch", screen)   # the selector's footer
                os.write(fd, b"\x1b")   # Esc leaves for the shell
                while time.monotonic() < deadline:
                    done, status = os.waitpid(pid, os.WNOHANG)
                    if done:
                        break
                    if select.select([fd], [], [], 0.2)[0]:
                        try:
                            os.read(fd, 65536)
                        except OSError:
                            pass
                self.assertEqual(done, pid)
                self.assertEqual(os.waitstatus_to_exitcode(status), 0)
            finally:
                try:
                    os.kill(pid, 9)
                    os.waitpid(pid, 0)
                except (ProcessLookupError, ChildProcessError):
                    pass
                os.close(fd)

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

    def test_provider_timestamps_accept_rfc3339_fractional_seconds(self):
        for fraction in ("1", "12", "123", "1234", "12345", "123456", "1234567", "12345678", "123456789"):
            with self.subTest(fraction=fraction):
                expected = picker._epoch("2030-01-01T00:00:00." + fraction[:6].ljust(6, "0") + "Z")
                self.assertEqual(picker._epoch("2030-01-01T00:00:00." + fraction + "Z"), expected)
                self.assertEqual(picker._epoch("2029-12-31T17:00:00." + fraction + "-07:00"), expected)
        self.assertEqual(picker._epoch("2030-01-01T00:00:00Z"), 1893456000)

    def test_antigravity_refresh_accepts_go_token_and_quota_timestamps(self):
        token = {"token": {"access_token": "fake", "expiry": "2030-01-01T00:00:00.85698-07:00"}}
        raw = "go-keyring-base64:" + picker.base64.b64encode(json.dumps(token).encode()).decode()
        quota = {"groups": [{"displayName": "Gemini", "buckets": [
            {"window": "5h", "remainingFraction": 0.75, "resetTime": "2030-01-01T00:00:00.123456789Z"}]}]}
        with patch.object(picker, "_keychain", return_value=raw), \
                patch.object(picker, "_http_json", return_value=quota) as http:
            windows = picker.usage_antigravity()
        http.assert_called_once()
        self.assertEqual(windows, [{"label": "5h", "pct": 25.0,
                                    "resets": picker._epoch("2030-01-01T00:00:00.123456Z")}])

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
            self.assertIn(f"alias usage='{home}/.local/bin/agent-picker --picker-usage'", text)
            self.assertIn(f"alias schedule={home}/.local/bin/schedule", text)
            self.assertIn(str(ROOT / "src/prompt_scheduler.py"), (home / ".local/bin/schedule").read_text())
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
