import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "src"))
import connections


class ConnectionMenuTests(unittest.TestCase):
    def test_setup_action_opens_the_provider_documentation(self):
        menu = connections.Menu([{"name": "Claude"}])
        url = connections.DOCS["Claude"][0]
        with patch.object(connections.webbrowser, "open") as open_page:
            self.assertIsNone(menu.handle("o"))
        open_page.assert_called_once_with(url)
        self.assertIn("setup guide", menu.notice)

    def test_sign_in_runs_only_after_explicit_action_and_uses_login_args(self):
        with tempfile.TemporaryDirectory() as folder:
            binary = Path(folder) / ("claude.exe" if os.name == "nt" else "claude")
            binary.write_text("provider executable placeholder", encoding="utf-8")
            binary.chmod(0o755)
            agent = {"name": "Claude", "argv": [str(binary), "--dangerously-skip-permissions",
                                                    "--model", "opus"]}
            menu = connections.Menu([agent])
            with patch.object(connections.subprocess, "call") as run_login:
                self.assertIsNone(menu.handle("j"))
                self.assertFalse(run_login.called)
                action = menu.handle("l")
                self.assertFalse(run_login.called)
            self.assertEqual(action, ("login", [str(binary), "auth", "login"]))
            self.assertNotIn("--dangerously-skip-permissions", action[1])
            self.assertNotIn("--model", action[1])
            self.assertNotIn("opus", action[1])

    def test_version_check_executes_only_version_without_interactive_stdin(self):
        menu = connections.Menu([{"name": "Codex", "argv": ["codex", "--dangerously-bypass-approvals-and-sandbox",
                                                                  "--model", "gpt-test"]}])
        result = type("Result", (), {"returncode": 0})()
        with patch.object(connections.subprocess, "run", return_value=result) as run:
            menu.check("C:/fake/codex.exe" if os.name == "nt" else "/fake/codex")
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["C:/fake/codex.exe", "--version"] if os.name == "nt"
                         else ["/fake/codex", "--version"])
        self.assertIs(kwargs["stdin"], connections.subprocess.DEVNULL)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", args[0])
        self.assertNotIn("--model", args[0])
        self.assertFalse(menu.busy)
        self.assertIn("CLI responds", menu.notice)


if __name__ == "__main__":
    unittest.main()
