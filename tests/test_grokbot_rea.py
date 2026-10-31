from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import grokbot as cli


def conversation(machine="m1"):
    convo = cli.Conversation.__new__(cli.Conversation)
    convo.cwd = "/tmp/project"
    convo.session = {"machine": machine, "where": "local"}
    return convo


class ReaHintTests(unittest.TestCase):
    def test_reverse_engineering_request_points_at_rea(self):
        for text in ("reverse engineer how Notes search works", "decompile this binary", "use REA on the app"):
            self.assertIn(cli.REA_HINT, conversation().prompt(text, "n"))

    def test_ordinary_request_has_no_rea_hint(self):
        self.assertNotIn(cli.REA_HINT, conversation().prompt("fix the failing tests in area.py", "n"))

    def test_cloud_session_has_no_rea_hint(self):
        self.assertNotIn(cli.REA_HINT, conversation(machine=None).prompt("reverse engineer this app", "n"))


if __name__ == "__main__":
    unittest.main()
