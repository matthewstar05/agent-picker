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
        agents = {a["name"]: a for a in settings.load_agents()}
        self.assertEqual(agents["Codex"]["argv"][1:], ["-m", "gpt-6.1-sol", "-c", "model_reasoning_effort=high", "--dangerously-bypass-approvals-and-sandbox"])
        self.assertIn("--dangerously-skip-permissions", agents["Claude"]["argv"])
        self.assertIn("--trust-all-tools", agents["Kiro"]["argv"])
        self.assertEqual(len(agents), 5)

    def test_local_platform_override(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "config.json"
            config.write_text(json.dumps({"agents": {"Cursor": {"argv": ["custom-cursor", "--custom"]}}}))
            with patch.dict(os.environ, {"AGENT_PICKER_CONFIG": str(config)}), patch.object(settings, "WINDOWS", True):
                agents = settings.load_agents()
                self.assertEqual(agents[2]["argv"], ["custom-cursor", "--custom"])
                self.assertEqual(agents[1]["argv"][0], "codex")

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
            original = json.loads((ROOT / "agents.json").read_text())[2]
            self.assertEqual(agents[2]["argv"], ["vendor-cursor"] + original["argv"][1:])
            self.assertEqual(agents[2]["executables"], [])

    def test_resolution_uses_cursor_fallback_without_generic_agent(self):
        seen = []
        def which(command):
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
                        lines = picker.render(selected, [True] * 5, 0.3, cols, rows)
                        self.assertLessEqual(len(lines), rows)
                        for line in lines:
                            self.assertLessEqual(picker.visible_len(line), cols, (cols, rows, selected))

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


if __name__ == "__main__":
    unittest.main()
