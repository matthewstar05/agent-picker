import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import usage_panel


def tab(*blocks):
    return {"layers": [{"blocks": [{"block_id": b} for b in blocks]}]}


class RexScopeTests(unittest.TestCase):
    """In Rex the usage panel counts agents by the pane they run in, so a card
    shows only for providers open on its own tab by default."""

    def setUp(self):
        usage_panel._rex_roots.clear()

    def test_scopes_follow_tab_and_session(self):
        views = {"session:here": {"windows": [tab("block:usage", "block:a", "block:b"), tab("block:c")]},
                 "session:other": {"windows": [tab("block:d")]}}
        roots = {"block:a": 10, "block:b": 20, "block:c": 30, "block:d": 40}
        procs = [(10, 1, ["zsh"]), (11, 10, ["codex"]), (20, 1, ["zsh"]), (21, 20, ["claude"]),
                 (30, 1, ["zsh"]), (31, 30, ["agy"]), (40, 1, ["zsh"]), (41, 40, ["cursor-agent"])]

        def fake(*args):
            if args[0] == "ls":
                return {"sessions": [{"session_id": s} for s in views]}
            if args[:2] == ("session", "inspect"):
                return views[args[2]]
            if args[-1] == "program_status":
                return {"records": [{"state": "working" if args[5] == "block:b" else "idle"}]}
            return {"child": {"pid": roots[args[5]]}}

        names = {"codex": "Codex", "claude": "Claude", "agy": "Antigravity", "cursor-agent": "Cursor"}
        with mock.patch.dict(os.environ, {"REX_SESSION": "session:here"}), \
                mock.patch.object(usage_panel, "rex_json", side_effect=fake), \
                mock.patch.object(usage_panel, "processes", return_value=procs), \
                mock.patch.object(usage_panel, "provider_of", side_effect=lambda p: names.get(p)), \
                mock.patch.object(usage_panel, "star_turns", return_value=[]):
            working = {}
            found = usage_panel.rex_sessions("block:usage", working)
        self.assertEqual(set(found["tab"]), {"Codex", "Claude"})
        self.assertEqual(set(found["workspace"]), {"Codex", "Claude", "Antigravity"})
        self.assertEqual(set(found["all"]), {"Codex", "Claude", "Antigravity", "Cursor"})
        # Claude's pane tells Rex it is working, so its card animates.
        self.assertEqual(working, {"Claude": {"block:b"}})
        self.assertEqual(found["tab"]["Claude"]["working"], 1)
        self.assertEqual(found["tab"]["Codex"]["working"], 0)


class RexScreenWorkingTests(unittest.TestCase):
    """Cursor and Kiro don't report their state to Rex, so their screens are
    read the way Herdr's detection rules read them."""

    def test_cursor_working_lines(self):
        read = usage_panel.cursor_screen_working
        self.assertTrue(read("plan\n  ⠋ Generating\n  ctrl+c to stop\n"))
        self.assertTrue(read("  ⠙ Reading files\n"))
        self.assertTrue(read("2 background tasks\n"))
        self.assertFalse(read("  → Add a follow-up\n  Claude Opus 5.5 300K Medium\n"))

    def test_an_approval_prompt_is_not_working(self):
        self.assertFalse(usage_panel.cursor_screen_working("⠋ Running\nRun this command?\nSkip (esc or n)\n"))

    def test_kiro_working_by_footer_or_title(self):
        self.assertTrue(usage_panel.kiro_screen_working("Kiro is working · type to steer\n"))
        self.assertTrue(usage_panel.kiro_screen_working("", "◐ kiro: thinking"))
        self.assertFalse(usage_panel.kiro_screen_working("> \n", "kiro"))

    def test_screen_is_read_only_without_a_working_status(self):
        calls = []

        def text(*args):
            calls.append(args)
            return "  ⠋ Generating\n"

        status = {"records": [{"state": "done", "app": "claude-code"}]}   # left by an earlier program
        with mock.patch.object(usage_panel, "rex_json", side_effect=lambda *a: status if a[-1] == "program_status"
                               else {"title": ""}), \
                mock.patch.object(usage_panel, "rex_text", side_effect=text):
            self.assertTrue(usage_panel.rex_working("s", "b", {"Cursor": 1}))
            self.assertFalse(usage_panel.rex_working("s", "b", {"Codex": 1}))
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
