import argparse
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
import grokbot as cli
import grokbot_where as where
import preferences
from test_grokbot import FakeGateway, MACHINE


def args(**kw):
    return argparse.Namespace(**dict({"cloud": False, "where": None}, **kw))


class WhereTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        env = patch.dict(os.environ, AGENT_PICKER_STATE_HOME=str(self.root / "state"), HERDR_ENV="")
        env.start()
        self.addCleanup(env.stop)
        prefs = patch.object(preferences, "PATH", self.root / "preferences.json")
        prefs.start()
        self.addCleanup(prefs.stop)
        preferences._values = None
        self.addCleanup(setattr, preferences, "_values", None)

    def sent_prompt(self, mode, cwd=None):
        gateway = FakeGateway()
        conversation = cli.Conversation(gateway, cli.Output("json"), cwd or self.root, MACHINE, None, timeout=1, poll=0)
        conversation.where = mode
        with contextlib.redirect_stdout(io.StringIO()):
            conversation.turn("update the README")
        return gateway.sent[0][1]

    def test_local_mode_keeps_the_existing_instructions(self):
        text = self.sent_prompt(where.LOCAL)
        self.assertIn("do not create a substitute project there", text)
        self.assertNotIn("Choose where to do this request", text)

    def test_auto_mode_lets_the_bot_choose_with_the_repository_facts(self):
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://user:secret@github.com/me/MSQE_Stuff.git"], check=True)
        (repo / "notes.md").write_text("draft")
        text = self.sent_prompt(where.AUTO, repo)
        self.assertIn("Choose where to do this request", text)
        self.assertIn("then commit and push", text)
        self.assertIn('"https://github.com/me/MSQE_Stuff.git"', text)
        self.assertNotIn("secret", text)
        self.assertIn("uncommitted local changes (1 paths)", text)
        self.assertIn("User request:\nupdate the README", text)

    def test_a_folder_outside_git_is_described_as_local_only(self):
        self.assertIn("exist only on this machine", where.repo_facts(str(self.root)))

    def test_mode_comes_from_flags_then_the_saved_conversation_then_the_setting(self):
        self.assertEqual(cli.choose_where(args(), None), where.LOCAL)
        preferences.put(where.WHERE.key, where.AUTO)
        self.assertEqual(cli.choose_where(args(), None), where.AUTO)
        self.assertEqual(cli.choose_where(args(cloud=True), None), where.CLOUD)
        self.assertEqual(cli.choose_where(args(where=where.LOCAL), None), where.LOCAL)
        self.assertEqual(cli.choose_where(args(), {"machine": "local"}), where.LOCAL)   # saved before this setting existed
        self.assertEqual(cli.choose_where(args(), {"machine": None}), where.CLOUD)

    def test_a_saved_conversation_switches_between_local_and_auto_but_not_to_the_cloud(self):
        session = {"machine": "local", "where": where.LOCAL}
        self.assertEqual(cli.choose_where(args(where=where.AUTO), session), where.AUTO)
        self.assertEqual(session["where"], where.AUTO)
        with self.assertRaises(cli.GrokError):
            cli.choose_where(args(cloud=True), session)

    def test_new_auto_conversation_falls_back_to_the_cloud_when_the_mac_is_offline(self):
        output = cli.Output("json")
        offline = cli.GrokError("This Mac is offline.")
        with patch.object(cli, "connect_machine", side_effect=offline):
            self.assertIsNone(cli.machine_for(FakeGateway(), None, where.AUTO, None, output))
            with self.assertRaises(cli.GrokError):
                cli.machine_for(FakeGateway(), None, where.LOCAL, None, output)

    def test_setting_is_in_every_settings_screen_and_the_picker_line(self):
        import agent_picker as ap
        import team
        import usage_panel   # registers it in the selector's and usage panel's settings
        self.assertIs(preferences.find(where.WHERE.key), ap.GROKBOT_WHERE)
        self.assertIn("ap.GROKBOT_WHERE", Path(team.__file__).read_text())   # herdr-ai --settings list
        grok = {"name": "Grok Bot", "detail": "Cursor Grok Bot usage · local machine tools"}
        preferences.put(where.WHERE.key, where.AUTO)
        self.assertEqual(where.detail(grok), "Cursor Grok Bot usage · local or cloud by request")

    def test_grok_bot_defaults_screen_holds_the_setting_in_the_picker_and_herdr_setup(self):
        import agent_picker as ap
        import team
        grok = next(a for a in ap.AGENTS if a["name"] == "Grok Bot")
        menu = where.Menu(grok)
        frame = cli.CONTROL.sub("", "\n".join(map(str, ap.render_defaults(menu, 100, 30, 0))))
        self.assertIn("Grok Bot defaults", frame)
        self.assertIn("Works on", frame)
        self.assertIn("this Mac", frame)
        self.assertNotIn("managed by this provider", frame)
        menu.handle("right")
        self.assertEqual(preferences.get(where.WHERE.key), where.AUTO)   # saved at once, shared with settings
        self.assertFalse(menu.changed)
        self.assertEqual(menu.handle("esc"), "save")
        with patch.object(team, "providers", return_value=[grok]):
            self.assertIsInstance(team.models_menu("Grok Bot"), where.Menu)

    def test_where_flag_is_validated_against_cloud_and_machine(self):
        for argv in (["--cloud", "--where", "auto", "-p", "x"], ["--where", "cloud", "--machine", "m", "-p", "x"]):
            with self.assertRaises(SystemExit), patch("sys.stderr"):
                cli.main(argv)


if __name__ == "__main__":
    unittest.main()
