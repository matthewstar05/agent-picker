import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import keep_awake
import preferences


class FakeAmphetamine:
    """Answers the AppleScript commands keep_awake sends, without touching the real app."""

    def __init__(self, active=False, remaining=0, trigger=False):
        self.active, self.remaining, self.trigger = active, remaining, trigger
        self.closed_display = False
        self.commands = []

    def __call__(self, command):
        self.commands.append(command)
        if command == "session is active":
            return "true" if self.active else "false"
        if command == "session time remaining":
            return str(self.remaining if self.active else -3)
        if command == "session is Trigger":
            return "true" if self.trigger else "false"
        if command.startswith("start new session"):
            self.active, self.remaining, self.trigger = True, 0, False
            return ""
        if command == "end session":
            self.active = False
            return ""
        if command == "closed display mode enabled":
            return "true" if self.closed_display else "false"
        if command == "enable closed display mode":
            self.closed_display = True
            return ""
        raise AssertionError(command)

    def started(self):
        return sum(c.startswith("start new session") for c in self.commands)

    def ended(self):
        return self.commands.count("end session")


@unittest.skipUnless(keep_awake.SUPPORTED, "Amphetamine is macOS only")
class KeepAwakeTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.working = []
        self.docked = False
        self.app = FakeAmphetamine()
        for patcher in (patch.object(preferences, "PATH", self.root / "preferences.json"),
                        patch.object(preferences, "_values", {}),
                        patch.object(keep_awake, "STATE", self.root / "keep-awake.json"),
                        patch.object(keep_awake, "LOCK", self.root / "keep-awake.lock"),
                        patch.object(keep_awake, "installed", return_value=True),
                        patch.object(keep_awake, "agents_working", side_effect=lambda: list(self.working)),
                        patch.object(keep_awake, "_external_display_on_ac", side_effect=lambda: self.docked),
                        patch.object(keep_awake, "_tell", side_effect=self.app),
                        patch.object(keep_awake, "ensure_service")):
            patcher.start()
            self.addCleanup(patcher.stop)
        preferences.put(keep_awake.KEEP_AWAKE.key, True)
        preferences.put(keep_awake.KEEP_AWAKE_DOCKED.key, True)

    def test_setting_is_listed_with_the_usage_panel_and_starts_off(self):
        self.assertEqual(keep_awake.KEEP_AWAKE.section, "Usage panel")
        self.assertIs(keep_awake.KEEP_AWAKE.default, False)
        self.assertEqual(keep_awake.KEEP_AWAKE_DOCKED.label, "Keep awake while docked")
        self.assertIs(keep_awake.KEEP_AWAKE_DOCKED.default, True)

    def test_a_working_agent_starts_a_session_that_ends_after_the_grace_period(self):
        self.working = ["claude in Herdr pane w1:p1"]
        state = keep_awake.tick(now=1000)
        self.assertTrue(state["owned"])
        self.assertEqual(self.app.started(), 1)
        self.assertIn("displaySleepAllowed:true", self.app.commands[1])
        self.assertTrue(self.app.closed_display, "the lid can close without sleeping")

        self.working = []
        keep_awake.tick(now=1000 + keep_awake.GRACE - 5)   # a gap between turns
        self.assertTrue(self.app.active)
        state = keep_awake.tick(now=1000 + keep_awake.GRACE + 5)
        self.assertFalse(state["owned"])
        self.assertFalse(self.app.active)
        self.assertEqual(self.app.ended(), 1)

    def test_records_what_kept_the_mac_awake(self):
        self.working = ["claude in Herdr pane w1:p1"]
        self.assertEqual(keep_awake.tick(now=1000)["reason"], ["claude in Herdr pane w1:p1"])
        self.working = []
        self.assertEqual(keep_awake.tick(now=1020)["reason"], [])

    def test_a_session_that_cant_be_ended_yet_is_retried_not_forgotten(self):
        self.working = ["claude in Herdr pane w1:p1"]
        keep_awake.tick(now=1000)
        self.working = []
        later = 1000 + keep_awake.GRACE + 5
        with patch.object(keep_awake, "_tell", return_value=None):   # Amphetamine busy or not answering
            self.assertTrue(keep_awake.tick(now=later)["owned"])
        self.assertTrue(self.app.active)
        self.assertFalse(keep_awake.tick(now=later + keep_awake.INTERVAL)["owned"])
        self.assertFalse(self.app.active)

    def test_looks_at_agents_at_most_every_interval(self):
        self.working = ["claude in Herdr pane w1:p1"]
        keep_awake.tick(now=1000)
        sent = len(self.app.commands)
        self.assertIsNone(keep_awake.tick(now=1000 + keep_awake.INTERVAL / 2))
        self.assertEqual(len(self.app.commands), sent)

    def test_your_own_session_is_never_started_over_or_ended(self):
        self.app.active, self.app.remaining = True, 3600
        self.working = ["claude in Herdr pane w1:p1"]
        keep_awake.tick(now=1000)
        self.working = []
        keep_awake.tick(now=1000 + keep_awake.GRACE + 5)
        self.assertEqual((self.app.started(), self.app.ended()), (0, 0))
        self.assertTrue(self.app.active)

    def test_a_timed_session_started_over_ours_is_left_running(self):
        self.working = ["claude in Herdr pane w1:p1"]
        keep_awake.tick(now=1000)
        self.app.remaining = 1800   # you started a 30 minute session over it
        self.working = []
        keep_awake.tick(now=1000 + keep_awake.GRACE + 5)
        self.assertEqual(self.app.ended(), 0)
        self.assertTrue(self.app.active)

    def test_a_session_ended_under_us_is_not_restarted_until_agents_idle(self):
        self.working = ["claude in Herdr pane w1:p1"]
        keep_awake.tick(now=1000)
        self.app.active = False   # ended from the menu bar, or on low battery
        keep_awake.tick(now=1020)
        keep_awake.tick(now=1040)
        self.assertEqual(self.app.started(), 1)

        self.working = []
        keep_awake.tick(now=1040 + keep_awake.GRACE + 5)
        self.working = ["claude in Herdr pane w1:p1"]
        keep_awake.tick(now=1040 + keep_awake.GRACE + 20)
        self.assertEqual(self.app.started(), 2)

    def test_turning_the_setting_off_ends_our_session_right_away(self):
        self.working = ["claude in Herdr pane w1:p1"]
        keep_awake.tick(now=1000)
        preferences.put(keep_awake.KEEP_AWAKE.key, False)
        state = keep_awake.tick(now=1001)
        self.assertFalse(state["owned"])
        self.assertFalse(self.app.active)

    def test_off_does_nothing_at_all(self):
        preferences.put(keep_awake.KEEP_AWAKE.key, False)
        self.working = ["claude in Herdr pane w1:p1"]
        self.assertIsNone(keep_awake.tick(now=1000))
        self.assertEqual(self.app.commands, [])

    def test_missing_amphetamine_is_never_asked(self):
        self.working = ["claude in Herdr pane w1:p1"]
        with patch.object(keep_awake, "installed", return_value=False):
            keep_awake.tick(now=1000)
        self.assertEqual(self.app.commands, [])

    def test_another_process_ticking_is_not_doubled_up(self):
        import fcntl
        self.working = ["claude in Herdr pane w1:p1"]
        with open(keep_awake.LOCK, "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            self.assertIsNone(keep_awake.tick(now=1000))
        self.assertEqual(self.app.started(), 0)

    def test_docked_external_display_keeps_an_idle_session_stable(self):
        self.docked = True
        first = keep_awake.tick(now=1000, force=True)
        second = keep_awake.tick(now=1000 + keep_awake.GRACE + keep_awake.INTERVAL, force=True)
        self.assertTrue(first["owned"] and second["owned"])
        self.assertEqual(second["reason"], ["external display on AC power"])
        self.assertEqual(self.app.started(), 1)
        self.assertIn("displaySleepAllowed:true", self.app.commands[1])

    def test_disconnect_ends_docked_session_after_grace(self):
        self.docked = True
        keep_awake.tick(now=1000, force=True)
        self.docked = False
        state = keep_awake.tick(now=1000 + keep_awake.GRACE + 5, force=True)
        self.assertFalse(state["owned"])
        self.assertEqual(self.app.ended(), 1)

    def test_probe_failure_preserves_last_docked_reason_and_retries_later(self):
        self.docked = True
        keep_awake.tick(now=1000, force=True)
        self.docked = None
        state = keep_awake.tick(now=1000 + keep_awake.GRACE + keep_awake.INTERVAL, force=True)
        self.assertTrue(state["owned"])
        self.assertIn("external display on AC power", state["reason"])
        self.assertEqual(self.app.started(), 1)
        self.assertEqual(self.app.ended(), 0)

    def test_battery_or_setting_off_does_not_keep_an_idle_dock_session(self):
        self.docked = False   # external display on battery is not a docked reason
        self.assertFalse(keep_awake.tick(now=1000, force=True).get("owned", False))
        self.docked = True
        preferences.put(keep_awake.KEEP_AWAKE_DOCKED.key, False)
        self.assertFalse(keep_awake.tick(now=1000 + keep_awake.INTERVAL, force=True).get("owned", False))
        self.assertEqual(self.app.started(), 0)

    def test_docked_mode_leaves_a_user_session_alone(self):
        self.docked = True
        self.app.active, self.app.remaining = True, 3600
        keep_awake.tick(now=1000, force=True)
        keep_awake.tick(now=1000 + keep_awake.GRACE + 5, force=True)
        self.assertEqual((self.app.started(), self.app.ended()), (0, 0))

    def test_main_setting_off_ends_a_docked_session(self):
        self.docked = True
        keep_awake.tick(now=1000, force=True)
        preferences.put(keep_awake.KEEP_AWAKE.key, False)
        self.assertFalse(keep_awake.tick(now=1001, force=True)["owned"])
        self.assertEqual(self.app.ended(), 1)

    def test_docked_option_falls_back_to_agent_activity_and_never_takes_over_a_user_session(self):
        preferences.put(keep_awake.KEEP_AWAKE_DOCKED.key, False)
        self.working = ["codex in Herdr pane w1:p1"]
        self.assertTrue(keep_awake.tick(now=1000, force=True)["owned"])

        self.app.active, self.app.remaining = True, 3600
        self.working = []
        self.docked = True
        state = keep_awake.tick(now=1000 + keep_awake.GRACE + 5, force=True)
        self.assertFalse(state["owned"])
        self.assertEqual((self.app.started(), self.app.ended()), (1, 0))
        self.assertTrue(self.app.active)


@unittest.skipUnless(keep_awake.SUPPORTED, "Amphetamine is macOS only")
class AgentsWorkingTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        for patcher in (patch.object(keep_awake, "state_dir", return_value=self.root),
                        patch.object(keep_awake, "_scheduled_running", return_value=[]),
                        patch.object(preferences, "PATH", self.root / "preferences.json"),
                        patch.object(preferences, "_values", {})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def herdr(self, panes):
        class Done:
            stdout = json.dumps({"result": {"panes": panes}})
        return patch.object(keep_awake.subprocess, "run", return_value=Done())

    def test_a_working_herdr_agent_counts_and_an_idle_one_does_not(self):
        with self.herdr([{"pane_id": "w1:p1", "agent": "claude", "agent_status": "idle"},
                         {"pane_id": "w1:p2", "agent": None, "agent_status": "working"}]):
            self.assertEqual(keep_awake.agents_working(), [])
        with self.herdr([{"pane_id": "w1:p3", "agent": "codex", "agent_status": "working"}]):
            self.assertEqual(keep_awake.agents_working(), ["codex in Herdr pane w1:p3"])

    def test_live_star_and_grok_bot_turns_count_only_while_their_process_runs(self):
        live = self.root / "grokbot" / "live"
        live.mkdir(parents=True)
        (live / "turn.json").write_text(json.dumps({"pid": os.getpid()}))
        with self.herdr([]):
            self.assertEqual(keep_awake.agents_working(), [f"Grok Bot turn (pid {os.getpid()})"])
            (live / "turn.json").write_text(json.dumps({"pid": 999999999}))
            self.assertEqual(keep_awake.agents_working(), [])

    def test_herdr_missing_is_not_working(self):
        with patch.object(keep_awake.subprocess, "run", side_effect=OSError):
            self.assertEqual(keep_awake.agents_working(), [])


if __name__ == "__main__":
    unittest.main()
