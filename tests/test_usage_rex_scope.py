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
            return {"child": {"pid": roots[args[5]]}}

        names = {"codex": "Codex", "claude": "Claude", "agy": "Antigravity", "cursor-agent": "Cursor"}
        with mock.patch.dict(os.environ, {"REX_SESSION": "session:here"}), \
                mock.patch.object(usage_panel, "rex_json", side_effect=fake), \
                mock.patch.object(usage_panel, "processes", return_value=procs), \
                mock.patch.object(usage_panel, "provider_of", side_effect=lambda p: names.get(p)), \
                mock.patch.object(usage_panel, "star_turns", return_value=[]):
            found = usage_panel.rex_sessions("block:usage", {})
        self.assertEqual(set(found["tab"]), {"Codex", "Claude"})
        self.assertEqual(set(found["workspace"]), {"Codex", "Claude", "Antigravity"})
        self.assertEqual(set(found["all"]), {"Codex", "Claude", "Antigravity", "Cursor"})


if __name__ == "__main__":
    unittest.main()
