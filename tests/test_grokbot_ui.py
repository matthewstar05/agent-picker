import io
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import grokbot_ui as ui


class ReplyWrapTest(unittest.TestCase):
    def test_marker_and_hanging_indent_within_width(self):
        lines = ui.wrap("word " * 40, 40).splitlines()
        self.assertTrue(lines[0].startswith("⏺ "))
        self.assertTrue(all(line.startswith("  ") for line in lines[1:]))
        self.assertTrue(all(len(line) < 40 for line in lines))

    def test_code_fences_are_indented_not_rewrapped(self):
        code = "x = " + "1 + " * 30 + "1"
        text = ui.wrap("Run:\n```\n" + code + "\n```", 40)
        self.assertIn("  " + code, text.splitlines())


class TurnScreenTest(unittest.TestCase):
    def screen(self):
        return ui.TurnScreen(stream=io.StringIO(), color=False)

    def test_typing_during_a_turn_is_kept_for_the_next_prompt(self):
        s = self.screen()
        for chunk in ("fads\r", "asdf\r", "asdx", "\x7f", "f"):
            s.keys(chunk)
        self.assertEqual(s.take(), "fads\nasdf\nasdf")
        self.assertEqual(s.take(), "")

    def test_lone_escape_interrupts_but_arrow_keys_do_not(self):
        s = self.screen()
        s.keys("\x1b[A")
        with self.assertRaises(KeyboardInterrupt):
            s.keys("\x1b")

    def test_working_line_is_erased_before_output(self):
        s = self.screen()
        s.draw()
        self.assertIn("esc to interrupt", s.out.getvalue())
        s.clear()
        self.assertTrue(s.out.getvalue().endswith("\r\x1b[2K"))
        self.assertFalse(s.shown)


if __name__ == "__main__":
    unittest.main()
