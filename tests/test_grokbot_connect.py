import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import grokbot as cli
from grokbot_gateway import GrokError

MAC = {"machineId": "local", "label": "test.local", "connected": False, "localToolPermission": "ask"}


class Roster:
    """Machines as Grok Bot reports them; connects after `after` more reads."""
    def __init__(self, rows, after=None):
        self.rows, self.after, self.reads = rows, after, 0

    def machines(self):
        self.reads += 1
        if self.after is not None and self.reads > self.after:
            return [dict(m, connected=True) for m in self.rows]
        return [dict(m) for m in self.rows]


class ConnectMachineTest(unittest.TestCase):
    def connect(self, roster, requested=None, app_exists=True, running=False):
        opened = []
        self.hidden = []
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(cli.socket, "gethostname", return_value="test.local"), \
                patch.object(cli.socket, "getfqdn", return_value="test.local"), \
                patch.object(cli.sys, "platform", "darwin"), patch.object(cli.time, "sleep"), \
                contextlib.redirect_stderr(io.StringIO()):
            app = Path(folder) / "Grok Bot.app"
            if app_exists:
                app.mkdir()
            try:
                return cli.connect_machine(roster, requested, wait=5, poll=0, app=app,
                                           opener=lambda argv, **_: opened.append(argv),
                                           running=lambda _: running,
                                           hide=lambda app: opened.append(["hide", str(app)])), opened
            except GrokError as exc:
                return exc, opened

    def test_opens_grok_bot_hidden_in_background_when_this_mac_is_offline(self):
        machine, opened = self.connect(Roster([MAC], after=2))
        self.assertEqual(machine["machineId"], "local")
        # The watcher starts first so it knows which app had focus before the launch.
        self.assertEqual(opened[0][0], "hide")
        self.assertEqual(opened[1][:4], ["open", "-g", "-j", "-a"])

    def test_an_app_the_user_already_opened_is_left_alone(self):
        machine, opened = self.connect(Roster([MAC], after=2), running=True)
        self.assertEqual((machine["machineId"], opened), ("local", []))

    def test_keep_hidden_watches_the_apps_own_bundle(self):
        spawned = []
        with tempfile.TemporaryDirectory() as folder:
            app = Path(folder) / "Grok Bot.app"
            cli.keep_hidden(app, spawn=lambda argv, **_: spawned.append(argv))
            self.assertEqual(spawned, [])          # no Info.plist, nothing to watch
            (app / "Contents").mkdir(parents=True)
            import plistlib
            with open(app / "Contents" / "Info.plist", "wb") as handle:
                plistlib.dump({"CFBundleIdentifier": "com.example.grok"}, handle)
            cli.keep_hidden(app, seconds=7, spawn=lambda argv, **_: spawned.append(argv))
        self.assertEqual(spawned[0][:3], ["osascript", "-l", "JavaScript"])
        self.assertEqual(spawned[0][-2:], ["com.example.grok", "7"])
        self.assertIn("hide", spawned[0][4])

    def test_connected_mac_opens_nothing(self):
        machine, opened = self.connect(Roster([dict(MAC, connected=True)]))
        self.assertEqual((machine["machineId"], opened), ("local", []))

    def test_another_hosts_machine_never_opens_the_app(self):
        error, opened = self.connect(Roster([dict(MAC, label="someone-else.local")]))
        self.assertIsInstance(error, GrokError)
        self.assertEqual(opened, [])

    def test_requested_remote_machine_never_opens_the_app(self):
        rows = [MAC, {"machineId": "remote", "label": "other.local", "connected": False}]
        error, opened = self.connect(Roster(rows), requested="remote")
        self.assertIsInstance(error, GrokError)
        self.assertEqual(opened, [])

    def test_gives_up_with_the_original_message_when_it_never_connects(self):
        error, opened = self.connect(Roster([MAC]))
        self.assertRegex(str(error), "No unique connected machine")
        self.assertEqual([argv[0] for argv in opened], ["hide", "open"])

    def test_missing_app_keeps_the_error(self):
        error, opened = self.connect(Roster([MAC]), app_exists=False)
        self.assertIsInstance(error, GrokError)
        self.assertEqual(opened, [])


if __name__ == "__main__":
    unittest.main()
