import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("install_windows", ROOT / "scripts/install-windows.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class WindowsInstallerTests(unittest.TestCase):
    def test_launcher_forwards_arguments_and_has_owned_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(installer, "ROOT", root):
                content = installer.launcher_content(Path(r"C:\Python\python.exe"), "agent_picker.py", [])
            self.assertIn(installer.MANAGED_MARKER, content)
            self.assertIn('"C:\\Python\\python.exe"', content)
            self.assertIn('"' + str(root / "src" / "agent_picker.py") + '" %*', content)
            self.assertIn("exit /b %errorlevel%", content)

    def test_selected_install_removes_only_owned_deselected_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bindir = root / "bin"
            bindir.mkdir()
            backups = root / "backups"
            owned = bindir / "usage.cmd"
            owned.write_text(f"@echo off\n{installer.MANAGED_MARKER}\npython \"{ROOT / 'src' / 'agent_picker.py'}\"\n")
            custom = bindir / "custom.cmd"
            custom.write_text("@echo off\necho custom\n")
            foreign = bindir / "foreign.cmd"
            foreign.write_text("rem managed by another checkout\n")
            lookalike = bindir / "herdr-ai.cmd"
            lookalike.write_text(f"{installer.MANAGED_MARKER} launcher\n{ROOT / 'src' / 'herdr_ai.py'}\n")
            unknown = bindir / "custom-owned.cmd"
            unknown.write_text(f"{installer.MANAGED_MARKER}\n{ROOT / 'src' / 'custom.py'}\n")
            with mock.patch.object(installer, "ROOT", ROOT):
                installer.remove_deselected(bindir, {"agent"}, backups)
            self.assertFalse(owned.exists())
            self.assertTrue(custom.exists())
            self.assertTrue(foreign.exists())
            self.assertTrue(lookalike.exists())
            self.assertTrue(unknown.exists())
            self.assertTrue((backups / owned.name).exists())

    def test_prepend_path_moves_existing_directory_to_front(self):
        result = installer.prepend_path(r"C:\Tools;C:\Windows;C:\AGENT\BIN", Path(r"c:\agent\bin"))
        self.assertEqual(result, r"c:\agent\bin;C:\Tools;C:\Windows")

    def test_main_writes_only_selected_launchers_and_preserves_custom_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bindir = root / "commands"
            (root / ".venv-windows" / "Scripts").mkdir(parents=True)
            (root / ".venv-windows" / "Scripts" / "python.exe").touch()
            local = root / "local"
            local.mkdir()
            (bindir).mkdir()
            (bindir / "custom.cmd").write_text("custom")
            (bindir / "usage.cmd").write_text(f"@echo off\n{installer.MANAGED_MARKER}\n\"{root / 'src' / 'agent_picker.py'}\"\n")
            with mock.patch.object(installer, "ROOT", root), \
                 mock.patch.object(installer.sys, "platform", "win32"), \
                 mock.patch.dict(os.environ, {"LOCALAPPDATA": str(local)}), \
                 mock.patch.object(installer.subprocess, "run"):
                self.assertEqual(installer.main(["--components", "fs", "--prefix", str(bindir), "--no-path"]), 0)
            self.assertTrue((bindir / "agent.cmd").exists())
            self.assertTrue((bindir / "agent-picker.cmd").exists())
            self.assertTrue((bindir / "fs.cmd").exists())
            self.assertTrue((bindir / "herdr-fs.cmd").exists())
            self.assertFalse((bindir / "usage.cmd").exists())
            self.assertFalse((bindir / "schedule.cmd").exists())
            self.assertTrue((bindir / "custom.cmd").exists())

    @unittest.skipUnless(os.name == "nt", "native .cmd forwarding requires Windows")
    def test_every_shared_launcher_forwards_arguments_and_exit_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source checkout"
            root.mkdir()
            src = root / "src"
            src.mkdir()
            bindir = Path(directory) / "my commands"
            bindir.mkdir()
            stub = ("import json, sys\nprint(json.dumps(sys.argv[1:]))\n"
                    "raise SystemExit(7)\n")
            for filename in {script for script, _ in installer.install_common.COMMANDS.values()}:
                (src / filename).write_text(stub, encoding="utf-8")
            arguments = ["two words", "$dollar", "it's fine"]
            with mock.patch.object(installer, "ROOT", root):
                for name, (script, extra) in installer.install_common.COMMANDS.items():
                    with self.subTest(command=name):
                        launcher = bindir / (name + ".cmd")
                        launcher.write_text(installer.launcher_content(Path(sys.executable), script, extra),
                                            encoding="utf-8")
                        result = subprocess.run([str(launcher), *arguments], capture_output=True, text=True)
                        import json
                        self.assertEqual(result.returncode, 7, result.stderr)
                        self.assertEqual(json.loads(result.stdout.strip()), [*extra, *arguments])


if __name__ == "__main__":
    unittest.main()
