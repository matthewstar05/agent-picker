import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import agent_picker as picker
import platform_runtime as runtime

PLAIN = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class SelectorMouseTests(unittest.TestCase):
    def frame(self, selected, cols, rows, count=5):
        agents = picker.AGENTS[:count]
        owners = []
        lines = picker.render(selected, [True] * count, 0.3, cols, rows, agents=agents, owners=owners)
        return agents, lines, owners

    def test_every_line_names_the_agent_drawn_on_it_at_any_size(self):
        for cols, rows in ((80, 30), (60, 16), (44, 9), (30, 6), (16, 4)):
            for selected in (0, 2, 4):
                with self.subTest(cols=cols, rows=rows, selected=selected):
                    agents, lines, owners = self.frame(selected, cols, rows)
                    self.assertEqual(len(owners), len(lines))
                    for line, who in zip(lines, owners):
                        if who is not None:
                            continue
                        text = PLAIN.sub("", line)
                        self.assertFalse([a["name"] for a in agents if a["name"] in text], text)
                    shown = [who for who in owners if who is not None]
                    self.assertEqual(shown, sorted(shown))              # top to bottom, in order
                    self.assertIn(selected, shown)                      # the selected agent is always in view
                    for who in set(shown):
                        first = PLAIN.sub("", lines[owners.index(who)])
                        if agents[who]["name"] in "".join(PLAIN.sub("", l) for l, w in zip(lines, owners) if w == who):
                            continue
                        self.fail(f"{agents[who]['name']} is not on its own rows: {first!r}")

    def test_a_click_lands_on_the_agent_under_it_once_the_panel_is_centered(self):
        cols, rows = 100, 40
        agents, lines, owners = self.frame(1, cols, rows)
        hits = picker.hit_rows(lines, owners, cols, rows)
        frame = picker.centered(lines, cols, rows)
        for row, first, last, who in hits:
            text = PLAIN.sub("", frame[row - 1])
            self.assertEqual(text[first - 1], "│")                      # the panel's own left edge
            self.assertEqual(text[last - 1], "│")
            self.assertEqual(picker.mouse_target(f"mouse:0:{first + 3}:{row}:press", hits), (who, 0))
        row, first, last, who = hits[0]
        self.assertEqual(picker.mouse_target(f"mouse:0:{first - 1}:{row}:press", hits), (None, 0))   # beside the panel
        self.assertEqual(picker.mouse_target(f"mouse:0:{first}:{row}:release", hits), (None, 0))
        self.assertEqual(picker.mouse_target(f"mouse:2:{first}:{row}:press", hits), (None, 0))       # right button
        self.assertEqual(picker.mouse_target(f"mouse:32:{first}:{row}:press", hits), (None, 0))      # a drag
        self.assertEqual(picker.mouse_target("mouse:64:1:1:press", hits), (None, -1))               # wheel up
        self.assertEqual(picker.mouse_target("mouse:65:1:1:press", hits), (None, 1))
        self.assertEqual(picker.mouse_target("mouse:x", hits), (None, 0))
        self.assertEqual({who for *_, who in hits}, set(range(len(agents))))

    def test_mouse_reports_are_read_without_the_other_extended_keys(self):
        self.assertEqual(runtime.decode_key(b"[", "<0;12;7M", mouse=True), ("mouse:0:12:7:press", 1))
        self.assertEqual(runtime.decode_key(b"[", "<0;12;7m", mouse=True), ("mouse:0:12:7:release", 1))
        self.assertEqual(runtime.decode_key(b"[", "<0;12;7M")[0], "")       # off: dropped, never read as Esc
        self.assertNotIn("focus", runtime.decode_key(b"[", "I", mouse=True)[0])
        self.assertEqual(runtime.decode_key(b"[", "I", extended=True)[0], "focus-in")
        self.assertEqual(runtime.decode_key(b"[", "A", mouse=True)[0], "up")

    def test_the_setting_is_on_by_default_and_can_be_turned_off(self):
        self.assertIs(picker.MOUSE.default, True)
        self.assertEqual(picker.MOUSE.choices, [True, False])


class AgyVersionTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.binary = self.root / "agy"
        self.binary.write_text("#!/bin/sh\n")
        self.calls = []
        for patcher in (patch.object(picker, "STATE", self.root / "state"),
                        patch.object(picker, "resolve_argv", lambda agent: [str(self.binary)]),
                        patch.object(picker.subprocess, "run", self.asked)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.answer = "agy 2.3.4\n"

    def asked(self, argv, **options):
        self.calls.append(options)
        return SimpleNamespace(stdout=self.answer)

    def test_agy_is_asked_once_per_build_and_outside_the_pane(self):
        self.assertEqual(picker._agy_version(), "2.3.4")
        self.assertEqual(picker._agy_version(), "2.3.4")
        self.assertEqual(len(self.calls), 1)                                # the answer is kept
        # Its own session: Herdr reads a pane's foreground job to name the agent in it.
        key = "creationflags" if sys.platform == "win32" else "start_new_session"
        self.assertTrue(self.calls[0][key])
        saved = json.loads((self.root / "state" / "agy-version.json").read_text())
        self.assertEqual(saved["version"], "2.3.4")
        self.answer = "agy 2.4.0\n"
        self.binary.write_text("#!/bin/sh\n# a newer build\n")              # an update is asked again
        self.assertEqual(picker._agy_version(), "2.4.0")
        self.assertEqual(len(self.calls), 2)

    def test_a_missing_or_silent_agy_falls_back(self):
        self.answer = "no version here"
        self.assertEqual(picker._agy_version(), "1.4.4")
        self.assertFalse((self.root / "state" / "agy-version.json").exists())
        self.binary.unlink()
        self.assertEqual(picker._agy_version(), "1.4.4")


if __name__ == "__main__":
    unittest.main()
