from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import grokbot_goal as goals


class VerdictTest(unittest.TestCase):
    def test_final_plain_line_decides(self):
        self.assertEqual(goals.verdict("Done.\nGOAL COMPLETE"), ("COMPLETE", ""))
        self.assertEqual(goals.verdict("Need it.\nGOAL BLOCKED: the API key"), ("BLOCKED", "the API key"))
        self.assertEqual(goals.verdict("GOAL COMPLETE\nStill working."), ("CONTINUES", ""))

    def test_marker_inside_an_open_code_block_is_not_a_verdict(self):
        self.assertEqual(goals.verdict("Example:\n```\nGOAL COMPLETE"), ("CONTINUES", ""))


class GoalTest(unittest.TestCase):
    def test_runs_until_complete_and_reports_it(self):
        goal = goals.Goal()
        goal.start("make the tests pass")
        self.assertIn("make the tests pass", goal.prompt())
        self.assertTrue(goal.finish("Step one.\nGOAL CONTINUES: run them"))
        self.assertIn("step 2", goal.prompt())
        self.assertFalse(goal.finish("All green.\nGOAL COMPLETE"))
        self.assertEqual(goal.state, "complete")
        self.assertTrue(goal.status().startswith("Goal achieved"))

    def test_blocked_and_step_cap_stop_the_loop(self):
        goal = goals.Goal()
        goal.start("deploy")
        goal.prompt()
        self.assertFalse(goal.finish("GOAL BLOCKED: credentials"))
        self.assertEqual((goal.state, goal.detail), ("blocked", "credentials"))
        goal.resume()
        goal.step = goals.MAX_STEPS - 1
        goal.prompt()
        self.assertFalse(goal.finish("More to do."))
        self.assertEqual(goal.state, "paused")
        self.assertTrue(goal.resume())
        self.assertEqual(goal.step, 0)

    def test_a_goal_running_at_exit_comes_back_paused(self):
        goal = goals.Goal()
        goal.start("refactor")
        goal.prompt()
        restored = goals.Goal(goal.data())
        self.assertEqual((restored.text, restored.state, restored.step), ("refactor", "paused", 1))
        restored.clear()
        self.assertIsNone(restored.data())


if __name__ == "__main__":
    unittest.main()
