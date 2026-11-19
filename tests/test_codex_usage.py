import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import agent_picker as ap


def record(stamp, used_5h, used_wk):
    return json.dumps({"timestamp": stamp, "type": "event_msg", "payload": {
        "type": "token_count", "rate_limits": {
            "limit_id": "codex",
            "primary": {"used_percent": used_5h, "window_minutes": 300, "resets_at": 2000},
            "secondary": {"used_percent": used_wk, "window_minutes": 10080, "resets_at": 3000}}}})


class CodexUsageTests(unittest.TestCase):
    def write(self, home, day, name, stamp, used_5h, used_wk, mtime):
        folder = Path(home) / "sessions" / "2026" / "11" / day
        folder.mkdir(parents=True, exist_ok=True)
        log = folder / f"rollout-{name}.jsonl"
        log.write_text(record(stamp, used_5h, used_wk) + "\n", encoding="utf-8")
        os.utime(log, (mtime, mtime))

    def test_a_resumed_old_session_does_not_hide_todays_reading(self):
        with tempfile.TemporaryDirectory() as home:
            # The old session was touched last, but its record is weeks old.
            self.write(home, "01", "old", "2026-10-11T05:44:51.708Z", 26.0, 4.0, mtime=1_795_000_900)
            self.write(home, "19", "new", "2026-11-19T18:49:54.107Z", 98.0, 15.0, mtime=1_795_000_800)
            with patch.dict(os.environ, {"CODEX_HOME": home}):
                windows = ap.usage_codex()
        self.assertEqual([(w["label"], w["pct"]) for w in windows], [("5h", 98.0), ("wk", 15.0)])

    def test_newest_file_wins_when_its_record_is_newest(self):
        with tempfile.TemporaryDirectory() as home:
            self.write(home, "18", "older", "2026-11-18T10:00:00Z", 40.0, 10.0, mtime=1_794_900_000)
            self.write(home, "19", "newer", "2026-11-19T10:00:00Z", 60.0, 12.0, mtime=1_795_000_000)
            with patch.dict(os.environ, {"CODEX_HOME": home}):
                windows = ap.usage_codex()
        self.assertEqual(windows[0]["pct"], 60.0)

    def test_no_logs_means_no_reading(self):
        with tempfile.TemporaryDirectory() as home:
            with patch.dict(os.environ, {"CODEX_HOME": home}):
                self.assertIsNone(ap.usage_codex())


if __name__ == "__main__":
    unittest.main()
