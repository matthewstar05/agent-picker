import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import herdr_return

SOCKET = "/fake.sock"
LAYOUTS = {SOCKET + "|w1:t1": {"panes": {"terminal": "w1:p1", "claude": "w1:p2", "agy": "w1:p4"}},
           "/other.sock|w1:t9": {"panes": {"terminal": "w1:p9"}}}
SHELL = 100
PICKER = {"pid": 7, "argv": ["/usr/bin/python3", str(herdr_return.herdr_ai.ENTRY), "--picker-return"]}


class FakeHerdr:
    def __init__(self):
        self.jobs = {}     # pane -> foreground processes besides the shell
        self.typed = []

    def process_info(self, pane):
        jobs = self.jobs.get(pane, [])
        return {"shell_pid": SHELL, "foreground_process_group_id": jobs[0]["pid"] if jobs else SHELL,
                "foreground_processes": jobs or [{"pid": SHELL, "argv": ["-zsh"]}]}

    def __call__(self, *args):
        if args[:2] == ("pane", "send-text"):
            self.typed.append((args[2], args[3]))
        return {}


def snapshot(agents, panes=("w1:p1", "w1:p2", "w1:p3", "w1:p4"), tab="w1:t1"):
    return {"panes": [{"pane_id": p, "tab_id": tab, "terminal_id": "term-" + p} for p in panes],
            "agents": [{"pane_id": p, "agent": a, "agent_session": {"value": "s-" + p}} for p, a in agents.items()]}


class ReturnTests(unittest.TestCase):
    def setUp(self):
        self.herdr = FakeHerdr()
        self.watch = herdr_return.Watch(self.herdr, SOCKET, load_state=lambda: LAYOUTS)

    def run_agent(self, pane, agent, by_picker=False):
        self.herdr.jobs[pane] = ([PICKER] if by_picker else []) + [{"pid": 50, "argv": [agent]}]
        self.watch.tick(snapshot({pane: agent}))

    def quit(self, pane):
        self.herdr.jobs.pop(pane, None)
        self.watch.tick(snapshot({}))

    def test_agent_typed_at_a_shell_reopens_the_selector_when_it_quits(self):
        self.run_agent("w1:p4", "agy")
        self.assertEqual(self.herdr.typed, [])
        self.quit("w1:p4")
        [(pane, line)] = self.herdr.typed
        self.assertEqual(pane, "w1:p4")
        self.assertTrue(line.rstrip("\n").endswith("--picker-return"))
        self.watch.tick(snapshot({}))
        self.assertEqual(len(self.herdr.typed), 1)   # once per quit

    def test_a_split_pane_in_the_tab_counts_too(self):
        self.run_agent("w1:p3", "codex")
        self.quit("w1:p3")
        self.assertEqual([p for p, _ in self.herdr.typed], ["w1:p3"])

    def test_agents_the_picker_runs_are_left_to_the_picker(self):
        self.run_agent("w1:p2", "claude", by_picker=True)
        self.quit("w1:p2")
        self.assertEqual(self.herdr.typed, [])

    def test_bottom_terminal_and_other_tabs_stay_shells(self):
        self.run_agent("w1:p1", "codex")
        self.quit("w1:p1")
        self.herdr.jobs["w9:p1"] = [{"pid": 50, "argv": ["codex"]}]
        self.watch.tick(snapshot({"w9:p1": "codex"}, panes=("w9:p1",), tab="w9:t1"))
        self.herdr.jobs.clear()
        self.watch.tick(snapshot({}, panes=("w9:p1",), tab="w9:t1"))
        self.assertEqual(self.herdr.typed, [])

    def test_waits_for_the_shell_prompt(self):
        self.run_agent("w1:p4", "agy")
        self.watch.tick(snapshot({}))    # herdr lost the agent, but agy is still exiting
        self.assertEqual(self.herdr.typed, [])
        self.quit("w1:p4")
        self.assertEqual(len(self.herdr.typed), 1)

    def test_a_replaced_pane_is_not_treated_as_the_old_one(self):
        self.run_agent("w1:p4", "agy")
        self.herdr.jobs.pop("w1:p4")
        replaced = snapshot({})
        replaced["panes"][3]["terminal_id"] = "term-new"
        self.watch.tick(replaced)
        self.assertEqual(self.herdr.typed, [])


if __name__ == "__main__":
    unittest.main()
