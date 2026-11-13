"""Partial Cursor reads keep all three limits visible without inventing usage."""

import json
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import agent_picker as ap
import preferences
import usage_panel as panel

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class CursorBarVisibilityTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for patcher in (patch.object(preferences, "PATH", Path(folder.name) / "preferences.json"),
                        patch.object(preferences, "_values", {}),
                        patch.dict(panel.USAGE, {}, clear=True),
                        patch.dict(panel.WORKING, {}, clear=True),
                        patch.dict(panel.CARDS, {}, clear=True)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.cursor = next(a for a in ap.AGENTS if a["name"] == "Cursor")
        self.view = panel.View([self.cursor], pinned=True)
        self.now = time.time()

    def text(self, lines):
        return ANSI.sub("", "\n".join("".join(line) if isinstance(line, tuple) else line for line in lines))

    def test_partial_read_displays_three_ordered_rows_without_changing_cached_data(self):
        reading = {"windows": [{"label": "gb", "pct": 10, "resets": self.now + 3600}], "fetched": self.now}
        panel.USAGE["Cursor"] = reading
        windows = ap.display_windows("Cursor", reading)
        self.assertEqual([w["label"] for w in windows], ["cr", "ex", "gb"])
        self.assertTrue(all(w["pct"] is None for w in windows[:2]))
        self.assertEqual(ap.numeric_windows(windows), reading["windows"])
        self.assertEqual(len(reading["windows"]), 1)
        for lines in (panel.render(self.view, 75, 22, 0),
                      [panel.compact_row(self.view, 0, 100, 0, self.now)]):
            text = self.text(lines)
            self.assertIn("cr", text)
            self.assertIn("ex", text)
            self.assertIn("gb", text)
            self.assertIn("90%", text)
            self.assertNotIn("100%", text)

    def test_no_reading_still_has_three_placeholder_rows(self):
        self.assertEqual(panel.card_rows(self.cursor), 4)
        text = self.text(panel.working_card(self.view, 0, 65, 0, self.now))
        for label in ("cr", "ex", "gb"):
            self.assertIn(label, text)
        self.assertNotIn("%", text)

    def test_settings_toggle_persists_and_invalidates_idle_card_immediately(self):
        panel.USAGE["Cursor"] = {"windows": [{"label": "gb", "pct": 10}], "fetched": self.now}
        before = self.text(panel.card(self.view, 0, 65, 0, self.now, rows=4))
        menu = preferences.Menu([panel.CURSOR_MODEL_BARS])
        menu.handle("right")
        saved = json.loads(preferences.PATH.read_text())
        self.assertEqual(saved[panel.CURSOR_MODEL_BARS.key], "available")
        after = self.text(panel.card(self.view, 0, 65, 0, self.now, rows=4))
        self.assertIn("cr", before)
        self.assertNotIn("cr", after)
        self.assertNotIn("ex", after)
        self.assertIn("gb", after)
        menu.handle("left")
        self.assertEqual(self.text(panel.card(self.view, 0, 65, 0, self.now, rows=4)), before)

    def test_model_setting_and_existing_grok_setting_control_their_own_rows(self):
        preferences.put(panel.CURSOR_GROK_TRANSIENT.key, True)
        self.assertEqual([w["label"] for w in ap.display_windows("Cursor", None)], ["cr", "ex"])
        preferences.put(panel.CURSOR_MODEL_BARS.key, "available")
        self.assertEqual(ap.display_windows("Cursor", None), [])
        reading = {"windows": [{"label": "cr", "pct": 25}, {"label": "gb", "pct": 40}]}
        self.assertEqual(ap.display_windows("Cursor", reading), reading["windows"])

    def test_failed_monthly_endpoint_keeps_weekly_reading_and_missing_model_rows(self):
        weekly = {"usagePercent": 10, "nextResetTimestampUtc": "2026-11-12T00:00:00Z"}
        with patch.object(ap, "_keychain", return_value="fake-token"), \
                patch.object(ap, "_http_json", side_effect=[OSError("offline"), weekly]):
            windows = ap.usage_cursor()
        self.assertEqual([w["label"] for w in windows], ["gb"])
        shown = ap.display_windows("Cursor", {"windows": windows})
        self.assertEqual([w["label"] for w in shown], ["cr", "ex", "gb"])
        self.assertEqual(shown[-1]["pct"], 10)

    def test_partial_fetch_keeps_last_model_readings_until_they_reset(self):
        previous = {"windows": [{"label": "cr", "pct": 32, "resets": self.now + 86400},
                                {"label": "ex", "pct": 54, "resets": self.now - 60},
                                {"label": "gb", "pct": 90, "resets": self.now + 3600}]}
        panel.USAGE["Cursor"] = previous
        fresh = [{"label": "gb", "pct": 12, "resets": self.now + 3600}]
        with patch.dict(ap.FETCHERS, {"Cursor": lambda: fresh}), patch.object(panel, "merge_cache"), \
                patch.object(panel, "record"):
            panel.fetch("Cursor")
        windows = {w["label"]: w["pct"] for w in panel.USAGE["Cursor"]["windows"]}
        self.assertEqual(windows, {"gb": 12, "cr": 32})
        self.assertEqual(ap.keep_missing("Claude", previous, fresh), fresh)


if __name__ == "__main__":
    unittest.main()
