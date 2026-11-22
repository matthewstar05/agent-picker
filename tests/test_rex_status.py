import base64
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import rex_status  # noqa: E402

REX = {"REX_BLOCK": "block:1", "REX_SESSION": "session:1"}


class FakeTTY(io.StringIO):
    def __init__(self, tty=True):
        super().__init__()
        self.tty = tty

    def isatty(self):
        return self.tty

    def fileno(self):
        return 99


class SequenceTests(unittest.TestCase):
    def test_reports_follow_the_protocol(self):
        self.assertEqual(rex_status.sequence("working", "grokbot"), "\x1b]7501;state=working:app=grokbot\x1b\\")
        self.assertEqual(rex_status.sequence("clear", "grokbot"), "\x1b]7501;state=clear\x1b\\")
        blocked = rex_status.sequence("blocked", "grokbot", msg="Allow?\nrm", kind="permission")
        body = blocked[len("\x1b]7501;"):-2]
        pairs = dict(pair.split("=", 1) for pair in body.split(":"))
        self.assertEqual(pairs["kind"], "permission")
        self.assertEqual(base64.b64decode(pairs["msg"]).decode(), "Allow?rm")

    def test_drops_what_rex_would_ignore(self):
        self.assertIsNone(rex_status.sequence("unknown", "grokbot"))
        self.assertIsNone(rex_status.sequence("idle", "bad app"))
        self.assertNotIn("kind=", rex_status.sequence("idle", "x", kind="permission"))


class ReportTests(unittest.TestCase):
    def test_writes_once_to_a_rex_terminal(self):
        with mock.patch.dict(os.environ, REX), mock.patch.object(rex_status.os, "write") as write:
            self.assertTrue(rex_status.report("idle", "cursor-agent", stream=FakeTTY()))
        write.assert_called_once_with(99, b"\x1b]7501;state=idle:app=cursor-agent\x1b\\")

    def test_silent_outside_rex_or_off_a_terminal(self):
        clean = {k: v for k, v in os.environ.items() if not k.startswith("REX_") and k != "TERM_PROGRAM"}
        with mock.patch.object(rex_status.os, "write") as write:
            with mock.patch.dict(os.environ, clean, clear=True):
                self.assertFalse(rex_status.report("idle", "x", stream=FakeTTY()))
            with mock.patch.dict(os.environ, REX):
                self.assertFalse(rex_status.report("idle", "x", stream=FakeTTY(tty=False)))
        write.assert_not_called()


class PrepareTests(unittest.TestCase):
    def test_cursor_gets_the_status_plugin_inside_rex(self):
        with tempfile.TemporaryDirectory() as folder, \
             mock.patch.dict(os.environ, {"AGENT_PICKER_STATE_HOME": folder}), \
             mock.patch.object(rex_status, "terminal_path", return_value="/dev/ttys042"):
            env = dict(REX)
            argv, app = rex_status.prepare({"name": "Cursor"}, ["cursor-cli", "--yolo"], env)
            self.assertEqual(app, "cursor-agent")
            self.assertEqual(env[rex_status.TTY_ENV], "/dev/ttys042")
            self.assertEqual(argv[:2], ["cursor-cli", "--yolo"])
            self.assertEqual(argv[2], "--plugin-dir")
            plugin = Path(argv[3])
            hooks = json.loads((plugin / "hooks/hooks.json").read_text())["hooks"]
            self.assertTrue(hooks["beforeSubmitPrompt"][0]["command"].endswith(" working"))
            self.assertTrue(hooks["stop"][0]["command"].endswith(" idle"))
            self.assertTrue((plugin / ".cursor-plugin/plugin.json").is_file())

    def test_other_agents_and_outside_rex_are_untouched(self):
        with mock.patch.object(rex_status, "cursor_plugin") as plugin:
            self.assertEqual(rex_status.prepare({"name": "Claude"}, ["claude"], dict(REX)), (["claude"], None))
            self.assertEqual(rex_status.prepare({"name": "Cursor"}, ["cursor-cli"], {}), (["cursor-cli"], None))
        plugin.assert_not_called()

    def test_hook_reports_to_the_pane_terminal(self):
        with tempfile.TemporaryDirectory() as folder:
            plugin = rex_status.cursor_plugin(folder)
            tty = Path(folder) / "tty"
            tty.write_text("")
            for state, reply in (("working", {"continue": True}), ("idle", {})):
                result = subprocess.run([sys.executable, "-I", str(plugin / "hooks/rex_status.py"), state],
                                        input='{"hook_event_name": "x"}', capture_output=True, text=True,
                                        env={**os.environ, rex_status.TTY_ENV: str(tty)})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), reply)
                self.assertEqual(tty.read_text(), f"\x1b]7501;state={state}:app=cursor-agent\x1b\\")


class GrokbotTests(unittest.TestCase):
    def test_states_map_to_rex(self):
        import grokbot
        convo = grokbot.Conversation.__new__(grokbot.Conversation)
        convo.output, convo.session = grokbot.Output(interactive=True), None
        sent = []
        with mock.patch.object(convo, "mark"), mock.patch.dict(os.environ, {"HERDR_ENV": ""}), \
             mock.patch.object(grokbot.rex_status, "report", side_effect=lambda *a, **k: sent.append((a, k))):
            for state in ("working", "blocked", "unknown", "idle"):
                convo.report(state)
            convo.output = grokbot.Output(interactive=False)
            convo.report("working")
        self.assertEqual([a[0] for a, _ in sent], ["working", "blocked", "idle", "idle"])
        self.assertEqual(sent[1][1]["kind"], "permission")


if __name__ == "__main__":
    unittest.main()
