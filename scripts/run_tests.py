"""Run the complete suite with a traceback if one native test stops making progress."""
import faulthandler
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class Result(unittest.TextTestResult):
    def startTest(self, test):
        super().startTest(test)
        # A C-level watchdog also catches a blocking native call that Python
        # cannot interrupt. All test data/processes belong to this CI job.
        faulthandler.dump_traceback_later(120, exit=True)

    def stopTest(self, test):
        faulthandler.cancel_dump_traceback_later()
        super().stopTest(test)


if __name__ == "__main__":
    faulthandler.enable()
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    result = unittest.TextTestRunner(verbosity=2, resultclass=Result).run(suite)
    raise SystemExit(not result.wasSuccessful())
