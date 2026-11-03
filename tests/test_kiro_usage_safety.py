"""Quota refreshes must never start Kiro's interactive account/integration flow."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import agent_picker as picker


class KiroUsageSafetyTests(unittest.TestCase):
    def test_background_usage_refresh_never_launches_a_kiro_process(self):
        with patch.object(picker.subprocess, "run") as run, \
                patch.object(picker, "resolve_argv") as resolve:
            self.assertIsNone(picker.usage_kiro())
        run.assert_not_called()
        resolve.assert_not_called()

    def test_failed_refresh_preserves_the_timestamp_and_cached_quota(self):
        cached = {"windows": [{"label": "mo", "pct": 12}], "fetched": 100}
        with patch.object(picker, "USAGE", {"Kiro": cached}), \
                patch.object(picker, "PENDING", {"Kiro"}), \
                patch.object(picker.usage_cache, "write") as write, \
                patch.object(picker.subprocess, "run") as run:
            picker._fetch("Kiro")
            self.assertEqual(picker.USAGE["Kiro"], cached)
            self.assertNotIn("Kiro", picker.PENDING)
        write.assert_not_called()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
