import io
import os
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import grokbot_ui as ui
from star_input import Editor, Wake


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


class Box:
    """Stands in for CursorInput.read: plays scripted keys, calling idle() like the editor."""
    def __init__(self, script):
        self.script, self.queued, self.working, self.reads = list(script), [], None, 0

    def read(self, initial="", idle=None, **_):
        self.reads += 1
        step = self.script.pop(0) if self.script else "idle"
        if step.startswith("type:"):
            return step[5:]                      # enter on that text
        if step.startswith("wake:"):
            raise Wake(step[5:], initial)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:       # wait for the turn the way the editor does
            woke = idle()
            if woke:
                raise Wake(woke, initial + "left")
            time.sleep(0.01)
        raise AssertionError("turn never finished")

    def draw(self):
        pass

    def _erase(self):
        pass


class WorkingTest(unittest.TestCase):
    def run_turn(self, script, job):
        box = Box(script)
        runner = ui.Working(box, out=io.StringIO())
        left = runner.run(lambda: job(runner))
        return box, runner, left

    def test_enter_queues_and_output_prints_above_the_box(self):
        done = threading.Event()

        def job(runner):
            runner.emit("⏺ reply")
            done.wait(2)
        box, runner, left = self.run_turn(["type:first", "type:second"], lambda r: (job(r)))
        done.set()
        self.assertEqual(box.queued, ["first", "second"])
        self.assertIn("⏺ reply", runner.out.getvalue())
        self.assertIn("Worked for", runner.out.getvalue())
        self.assertIsNone(runner.error)
        self.assertEqual(left, "left")

    def test_interrupt_reaches_the_turn_through_wait(self):
        def job(runner):
            for _ in range(500):
                runner.wait(0.01)
        _, runner, _ = self.run_turn(["wake:interrupt"], job)
        self.assertIsInstance(runner.error, KeyboardInterrupt)

    def test_slash_stop_interrupts_instead_of_queueing(self):
        def job(runner):
            for _ in range(500):
                runner.wait(0.01)
        box, runner, _ = self.run_turn(["type:/stop"], job)
        self.assertIsInstance(runner.error, KeyboardInterrupt)
        self.assertEqual(box.queued, [])

    def test_approval_question_is_asked_on_the_main_thread(self):
        answers = []
        with patch("builtins.input", return_value="y") as ask:
            _, runner, _ = self.run_turn([], lambda r: answers.append(r.ask("Allow? ")))
        ask.assert_called_once_with("Allow? ")
        self.assertEqual(answers, ["y"])


class WorkingKeysTest(unittest.TestCase):
    def keys(self, pressed, text="", working=True):
        editor = ui.CursorInput([("help", "", "show commands")])
        editor.text, editor.pos, editor.working = text, len(text), (1.0 if working else None)
        with patch.object(Editor, "keys", lambda self, fd, idle: iter(pressed)):
            return list(editor.keys(0, None))

    def test_tab_queues_and_esc_interrupts_while_working(self):
        self.assertEqual(self.keys(["tab", "esc"], "hello"), ["enter", ("wake", "interrupt")])

    def test_ctrl_c_interrupts_only_an_empty_box(self):
        self.assertEqual(self.keys(["ctrl-c"]), [("wake", "interrupt")])
        self.assertEqual(self.keys(["ctrl-c"], "draft"), ["ctrl-c"])

    def test_keys_are_unchanged_between_turns(self):
        self.assertEqual(self.keys(["tab", "esc"], "hello", working=False), ["tab", "esc"])


class ImageTokenTest(unittest.TestCase):
    """An [Image #n] placeholder deletes as one unit, so a half-erased one can't
    be sent as plain text in place of the image."""

    def type(self, initial, script, images=(1,)):
        editor = ui.CursorInput([])
        editor.images = {n: f"/tmp/image-{n}.png" for n in images}
        def keys(fd, idle):
            for key in script:
                if isinstance(key, int):
                    editor.pos = key        # place the cursor
                    continue
                yield key
            yield ("wake", "done")
        with patch("star_input.termios"), patch("star_input.tty"), patch("sys.stdin"), \
                patch("sys.stdout", io.StringIO()), patch.object(editor, "keys", keys):
            with self.assertRaises(Wake) as caught:
                editor.read(initial=initial)
        return caught.exception.text, editor.pos

    def test_backspace_at_the_end_of_the_token_deletes_it_whole(self):
        self.assertEqual(self.type("look [Image #1]", ["backspace"]), ("look ", 5))

    def test_space_after_a_pasted_image_goes_first_then_the_token(self):
        self.assertEqual(self.type("[Image #1] ", ["backspace"])[0], "[Image #1]")
        self.assertEqual(self.type("[Image #1] ", ["backspace", "backspace"])[0], "")

    def test_backspace_and_delete_inside_the_token_remove_it_whole(self):
        self.assertEqual(self.type("a [Image #1] b", [8, "backspace"]), ("a  b", 2))
        self.assertEqual(self.type("a [Image #1] b", [2, "delete"]), ("a  b", 2))

    def test_kill_word_into_the_token_removes_it_whole(self):
        self.assertEqual(self.type("see [Image #12]", ["kill-word"], images=(12,))[0], "see ")

    def test_ordinary_text_still_deletes_one_character(self):
        self.assertEqual(self.type("[Image] #1]", ["backspace"])[0], "[Image] #1")
        self.assertEqual(self.type("abc", ["backspace"])[0], "ab")


class SelectionTest(unittest.TestCase):
    """Shift+moves select text, as in Claude Code; one backspace removes all of it."""

    type = ImageTokenTest.type

    def test_shift_sequences_from_the_terminal(self):
        read, write = os.pipe()
        os.write(write, b"\x1b[1;2Da\x1b[1;2H\x1b[1;4C\x1b[1;2A\x1b[D")
        os.close(write)
        with patch("star_input.select.select", lambda r, w, x, t=None: (r, [], [])):
            keys = list(Editor([]).keys(read, None))
        os.close(read)
        self.assertEqual(keys, ["shift-left", ("text", "a"), "shift-home", "shift-word-right", "shift-up", "left", "eof"])

    def test_one_backspace_deletes_a_shift_selection(self):
        self.assertEqual(self.type("hello world", ["shift-word-left", "backspace"]), ("hello ", 6))
        self.assertEqual(self.type("hello world", ["shift-home", "backspace"]), ("", 0))
        self.assertEqual(self.type("hello world", [0, "shift-right", "shift-right", "delete"]), ("llo world", 0))

    def test_select_everything_across_lines(self):
        self.assertEqual(self.type("one\ntwo\nthree", ["shift-up", "shift-up", "shift-home", "backspace"]), ("", 0))

    def test_typing_replaces_the_selection(self):
        self.assertEqual(self.type("hello world", ["shift-word-left", ("text", "there")]), ("hello there", 11))
        self.assertEqual(self.type("a b", ["shift-left", ("paste", "x\ny")]), ("a x\ny", 5))

    def test_a_plain_move_collapses_the_selection(self):
        self.assertEqual(self.type("hello", ["shift-home", "right", "backspace"]), ("hell", 4))
        self.assertEqual(self.type("hello", ["shift-home", "left", "delete"]), ("ello", 0))

    def test_a_selection_touching_an_image_removes_it_whole(self):
        self.assertEqual(self.type("a [Image #1] b", ["shift-left", "shift-left", "shift-left", "backspace"]), ("a ", 2))

    def test_selected_text_is_highlighted(self):
        editor = ui.CursorInput([])
        editor.text, editor.pos, editor.anchor = "hello world", 11, 6
        editor.wrap(2, 80)
        painted = editor.paint(0, "hello world")
        self.assertIn(editor.bg("select", editor.fg("pick", "world")), painted)
        editor.anchor = None
        self.assertEqual(editor.paint(0, "hello world"), editor.fg("text", "hello world"))


if __name__ == "__main__":
    unittest.main()
