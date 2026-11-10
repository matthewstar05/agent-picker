"""Focused tests for routing one file-browser panel without touching its siblings."""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).parent))
import herdr_ai
from test_herdr_ai import FakeHerdr, INSIDE


class AssignPanelTests(unittest.TestCase):
    def setUp(self):
        self.state_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.state_dir.cleanup)
        self.fake = FakeHerdr()
        self.herdr = herdr_ai.Herdr("herdr", "test")
        self.cwd = str(Path(self.state_dir.name) / "target")
        Path(self.cwd).mkdir()
        with patch.object(herdr_ai.subprocess, "run", self.fake), patch.object(herdr_ai, "sidebar_update"):
            self.panes = herdr_ai.build(
                self.herdr, "w9:p1", self.state_dir.name, "Claude",
                {"lead": "Claude", "gemini": False, "mode": herdr_ai.OFF})
        self.fake.start_agents()
        for role, pane in self.panes.items():
            if role not in ("terminal", "usage"):
                self.fake.panes[pane]["cwd"] = self.state_dir.name
        sidebar = patch.object(herdr_ai, "sidebar_update")
        sidebar.start()
        self.addCleanup(sidebar.stop)
        self.key = herdr_ai.tab_key("w9:t1")
        self.entry = {
            "panes": self.panes,
            "terminals": {pane: "term_" + pane for pane in self.panes.values()},
            "team": "Claude", "lead": "Claude", "gemini": False,
            "mode": herdr_ai.OFF,
        }
        self.state_patch = patch.object(herdr_ai, "load_state", return_value={self.key: self.entry})
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.env_patch = patch.dict(os.environ, INSIDE)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def assign(self, number):
        self.stop_calls = []
        with patch.object(herdr_ai.subprocess, "run", self.fake), \
                patch.object(herdr_ai, "stop_panes", side_effect=lambda h, panes: self.stop_calls.append(panes)):
            return herdr_ai.assign_panel_main([str(number)], herdr="herdr", cwd=self.cwd)

    def test_visual_team_panel_mapping(self):
        self.assertEqual(herdr_ai.PANEL_ROLES["team"], ("crew1", "crew2", "lead", "crew3", "crew4"))
        self.assertEqual(herdr_ai.PANEL_ROLES["Standard"], ("agy", "codex", "claude", "picker", "cursor"))

    def test_assign_routes_only_the_selected_panel_and_leaves_usage_alone(self):
        target = self.panes["crew2"]
        other_agents = {self.panes[role] for role in ("lead", *herdr_ai.CREW) if role != "crew2"}
        result = self.assign(2)
        self.assertEqual(result, 0)
        runs = self.fake.runs()
        self.assertEqual(set(runs), {target})
        self.assertIn("cd " + self.cwd, runs[target][0])
        self.assertIn("--picker-launch Claude", runs[target][1])
        self.assertFalse(other_agents & set(runs))
        self.assertNotIn(self.panes["usage"], runs)
        self.assertEqual(self.stop_calls, [[target]])
        stopped = {call[2] for call in self.fake.calls if call[:2] == ["pane", "send-text"] and call[3] == "\x03\x15"}
        self.assertEqual(stopped, {target})

    def test_working_or_blocked_panel_is_retained_with_nonzero_status(self):
        for status in ("working", "blocked"):
            with self.subTest(status=status):
                self.fake.calls.clear()
                self.fake.panes[self.panes["crew1"]]["agent_status"] = status
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    self.assertEqual(self.assign(1), 1)
                self.assertIn("not moved", err.getvalue())
                self.assertFalse([call for call in self.fake.calls if call[:2] in (["pane", "send-text"], ["pane", "send-keys"])])
                self.fake.panes[self.panes["crew1"]]["agent_status"] = "idle"

    def test_missing_panel_identity_and_no_layout_return_errors_without_mutation(self):
        del self.entry["panes"]["crew4"]
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.assign(5), 3)
        self.assertIn("panel 5 (crew4) is not present", err.getvalue())
        self.assertFalse([call for call in self.fake.calls if call[:2] in (["pane", "send-text"], ["pane", "send-keys"])])

        self.state_patch.stop()
        no_layout = patch.object(herdr_ai, "load_state", return_value={})
        no_layout.start()
        self.addCleanup(no_layout.stop)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.assign(1), 3)
        self.assertIn("no herdr-ai layout", err.getvalue())

    def test_argument_validation_and_outside_herdr_are_errors(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(herdr_ai.assign_panel_main([], herdr=self.fake, cwd=self.cwd), 2)
        self.assertIn("Usage:", err.getvalue())
        with patch.dict(os.environ, {"HERDR_ENV": "0", "HERDR_PANE_ID": ""}):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(herdr_ai.assign_panel_main(["1"], herdr=self.fake, cwd=self.cwd), 2)
            self.assertIn("must run inside herdr", err.getvalue())


if __name__ == "__main__":
    unittest.main()
