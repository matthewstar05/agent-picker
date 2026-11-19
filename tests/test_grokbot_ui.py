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


class ByteTerminal:
    """A terminal input source backed by bytes, without an OS pipe or TTY."""

    def __init__(self, data=b""):
        self.data = bytearray(data)
        self.eof = False

    def wait(self, _seconds):
        return bool(self.data) or not self.eof

    def read_bytes(self, size):
        if not self.data:
            self.eof = True
            return b""
        result = bytes(self.data[:size])
        del self.data[:size]
        return result


class NullTerminal:
    """Context-only Terminal mock for Editor.read tests with scripted keys."""

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class ReplyWrapTest(unittest.TestCase):
    def test_marker_and_hanging_indent_within_width(self):
        lines = ui.wrap("word " * 40, 40).splitlines()
        self.assertTrue(lines[0].startswith("• "))
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
        with patch("star_input.Terminal", return_value=NullTerminal()), patch("sys.stdin"), \
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
        editor = Editor([])
        editor.terminal = ByteTerminal(b"\x1b[1;2Da\x1b[1;2H\x1b[1;4C\x1b[1;2A\x1b[D")
        keys = list(editor.keys(0, None))
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

    # The box's text row sits on screen row 10: the terminal answers the cursor query
    # with row 10, and the cursor is on the box's first text line.
    def press(self, col, row=10):
        return ("mouse", (0, 3 + col, row, True))

    def test_dragging_with_the_mouse_selects_and_backspace_deletes_it(self):
        script = [self.press(0), ("report", (10, 14)), ("mouse", (32, 3 + 4, 10, True)),
                  ("mouse", (0, 3 + 4, 10, False)), "backspace"]
        self.assertEqual(self.type("hello world", script), (" world", 0))

    def test_dragging_backward_and_typing_replaces(self):
        script = [self.press(10), ("report", (10, 14)), ("mouse", (32, 3 + 6, 10, True)),
                  ("mouse", (0, 3 + 6, 10, False)), ("text", "W")]
        self.assertEqual(self.type("hello world", script), ("hello Wd", 7))

    def test_a_click_moves_the_cursor_and_a_double_click_picks_the_word(self):
        self.assertEqual(self.type("hello world", [self.press(2), ("report", (10, 14)), ("text", "X")]),
                         ("heXllo world", 3))
        double = [self.press(7), ("report", (10, 14)), ("mouse", (0, 10, 10, False)), self.press(7),
                  ("report", (10, 14)), ("mouse", (0, 10, 10, False)), "backspace"]
        self.assertEqual(self.type("hello world", double), ("hello ", 6))

    def test_mouse_reports_are_parsed_and_wheel_is_ignored(self):
        editor = Editor([])
        editor.terminal = ByteTerminal(b"\x1b[<0;5;7M\x1b[<32;6;7M\x1b[<0;6;7m\x1b[<64;5;7M")
        keys = list(editor.keys(0, None))
        self.assertEqual(keys, [("mouse", (0, 5, 7, True)), ("mouse", (32, 6, 7, True)),
                                ("mouse", (0, 6, 7, False)), ("mouse", (64, 5, 7, True)), "eof"])

    def test_the_mouse_is_captured_only_while_there_is_text(self):
        editor = ui.CursorInput([])
        out = io.StringIO()
        with patch("sys.stdout", out):
            editor.reading, editor.drawn_rows, editor.cursor_row = True, 0, 0
            editor.hint, editor.notice = (lambda: ("", "")), None
            editor.text, editor.pos = "", 0
            editor.draw()
            self.assertNotIn("\x1b[?1002h", out.getvalue())
            editor.text, editor.pos = "hi", 2
            editor.draw()
            self.assertIn("\x1b[?1002h", out.getvalue())
            editor.text, editor.pos = "", 0
            editor.draw()
            self.assertIn("\x1b[?1002l", out.getvalue())

    def test_selected_text_is_highlighted(self):
        editor = ui.CursorInput([])
        editor.text, editor.pos, editor.anchor = "hello world", 11, 6
        editor.wrap(2, 80)
        painted = editor.paint(0, "hello world")
        self.assertIn(editor.bg("selection", editor.fg("selection_text", "world")), painted)
        self.assertNotEqual(ui.COLORS["selection"], ui.COLORS["select"])   # visible on the box's background
        editor.anchor = None
        self.assertEqual(editor.paint(0, "hello world"), editor.fg("text", "hello world"))


if __name__ == "__main__":
    unittest.main()


class Pane(io.StringIO):
    def __init__(self, tty):
        super().__init__()
        self.tty = tty

    def isatty(self):
        return self.tty


class BannerTest(unittest.TestCase):
    def shown(self, width=100, tty=True, **kwargs):
        import grokbot
        pane = Pane(tty)
        with patch.object(grokbot.shutil, "get_terminal_size", return_value=os.terminal_size((width, 12))):
            grokbot.banner(kwargs.pop("cwd", "/work"), out=pane, **kwargs)
        return pane.getvalue()

    def test_no_line_reaches_the_last_column_at_any_width(self):
        import grokbot
        from star_input import cell_width
        for width in (20, 32, 40, 64, 80, 120):
            with self.subTest(width=width):
                shown = self.shown(width, cwd="/projects/" + "界" * 80, session={"id": "x" * 36}, mode="auto")
                lines = grokbot.clean(shown).splitlines()
                self.assertTrue(all(sum(cell_width(ch) for ch in line) < width for line in lines))
                self.assertIn("╭", shown)
                self.assertEqual(sum("▀" in line or "▄" in line for line in lines[:12]) >= 3, True)  # the mascot

    def test_header_returns_to_column_zero_even_without_terminal_newline_translation(self):
        self.assertNotIn("\n", self.shown().replace("\r\n", ""))

    def test_a_codex_style_card_holds_the_mascot_beside_name_model_and_folder(self):
        import grokbot
        shown = self.shown(100, cwd=Path.home() / "project", mode="local")
        # Screen and scrollback cleared and the cursor homed before anything is drawn.
        self.assertTrue(shown.startswith(grokbot.FRESH_SCREEN))
        lines = grokbot.clean(shown[len(grokbot.FRESH_SCREEN):]).splitlines()
        self.assertEqual(lines[0], "")      # headroom between the pane's top edge and the card
        self.assertTrue(lines[1].startswith("╭") and lines[1].endswith("╮"))
        self.assertIn("Grok Bot Agent (v" + grokbot.VERSION + ")", lines[2])
        self.assertTrue(any("model:" in line and "Grok Bot · Auto" in line for line in lines))
        self.assertTrue(any("directory:" in line and "~" + str(Path("/project")) in line for line in lines))
        art = [grokbot.clean(row) for row in grokbot.mascot()]
        self.assertEqual(lines[2][2:2 + grokbot.MASCOT_WIDTH], art[0])     # beside the name
        self.assertTrue(any(line.lstrip().startswith("/goal - ") for line in lines))

    def test_a_narrow_pane_puts_the_mascot_above_the_card(self):
        import grokbot
        lines = grokbot.clean(self.shown(30)).splitlines()
        self.assertEqual(lines[1:5], [grokbot.clean(row) for row in grokbot.mascot()])
        self.assertTrue(lines[6].startswith("╭"))

    def test_piped_output_gets_no_escape_codes_and_keeps_the_session(self):
        shown = self.shown(tty=False, session={"id": "abc"})
        self.assertNotIn("\x1b", shown)
        self.assertTrue(any("session:" in line and "abc" in line for line in shown.splitlines()))

    def test_mascot_rows_are_even_and_mirror_symmetric(self):
        import grokbot
        self.assertEqual(len(grokbot.MASCOT) % 2, 0)
        self.assertTrue(all(len(row) == grokbot.MASCOT_WIDTH and row == row[::-1] for row in grokbot.MASCOT))
        self.assertTrue(set("".join(grokbot.MASCOT)) <= set(grokbot.MASCOT_COLORS) | {"."})


class ResizeTest(unittest.TestCase):
    """A narrower terminal rewraps the drawn box, so the next draw must climb the
    rewrapped rows to reach its top instead of leaving old rows behind."""

    def draw(self, editor, width):
        out = io.StringIO()
        with patch("sys.stdout", out), patch("shutil.get_terminal_size", return_value=os.terminal_size((width, 30))):
            editor.draw()
        return out.getvalue()

    def editor(self):
        editor = ui.CursorInput([])
        editor.reading, editor.drawn_rows, editor.cursor_row = True, 0, 0
        editor.hint, editor.notice = (lambda: ("Grok Bot · Auto", "Local · ask")), None
        editor.text, editor.pos = "hello", 5
        return editor

    def test_rows_after_reflow(self):
        from star_input import rows_after_reflow
        # Three 99-cell lines above the cursor, on a 40-column terminal: 3 rows each.
        self.assertEqual(rows_after_reflow([99, 99, 99, 99], 3, 7, 40), 9)
        self.assertEqual(rows_after_reflow([99, 99, 99, 99], 3, 45, 40), 10)
        self.assertEqual(rows_after_reflow([0, 10], 1, 0, 40), 1)    # an empty line still takes a row

    def test_same_width_climbs_the_drawn_rows(self):
        editor = self.editor()
        self.draw(editor, 100)
        self.assertTrue(self.draw(editor, 100).startswith(f"\x1b[{editor.cursor_row}A\r\x1b[J"))

    def test_a_narrower_terminal_climbs_the_rewrapped_rows(self):
        editor = self.editor()
        self.draw(editor, 100)
        above = editor.drawn_widths[:editor.cursor_row]
        self.assertTrue(all(width == 99 for width in above))       # full-width edges and rows
        expected = sum(-(-width // 40) for width in above) + editor.cursor_col // 40
        self.assertTrue(self.draw(editor, 40).startswith(f"\x1b[{expected}A\r\x1b[J"))
        self.assertEqual(editor.drawn_columns, 40)
        self.assertTrue(all(width <= 39 for width in editor.drawn_widths))

    def test_the_editor_redraws_on_resize_without_a_key(self):
        editor = self.editor()
        self.draw(editor, 100)
        editor.terminal = ByteTerminal()
        editor.terminal.eof = False
        editor.terminal.wait = lambda _seconds: False
        with patch("shutil.get_terminal_size", return_value=os.terminal_size((60, 30))):
            self.assertEqual(next(editor.keys(0, None)), "resize")


def plain(text):
    from star_input import ANSI
    return ANSI.sub("", text)


class GoalHighlightTest(unittest.TestCase):
    def test_goal_command_is_highlighted_in_the_box_and_menu(self):
        editor = ui.CursorInput([("goal", "<objective>", "keep working"), ("help", "", "show commands")])
        editor.text, editor.pos = "/goal fix the tests", 19
        editor.wrap(2, 80)
        painted = editor.paint(0, editor.text)
        self.assertTrue(painted.startswith(ui.chip("/goal")))
        self.assertIn("fix the tests", painted)
        editor.text, editor.pos = "/goals", 6        # another word is not the command
        self.assertFalse(editor.paint(0, "/goals").startswith(ui.chip("/goal")))
        editor.text, editor.pos = "/help", 5
        self.assertEqual(editor.paint(0, "/help"), editor.fg("text", "/help"))

    def test_working_line_carries_the_goal_badge(self):
        editor = ui.CursorInput([])
        editor.working = time.monotonic()
        self.assertNotIn("goal", plain(editor.working_line(80)))
        editor.badge = "goal · step 3"
        line = editor.working_line(80)
        self.assertTrue(line.startswith(ui.chip(" ◎ goal · step 3 ")))
        self.assertIn("Working", plain(line))

    def test_goal_lines_fit_the_pane(self):
        from star_input import visible_width
        line = ui.goal_line(" ◎ /goal · step 1 ", "word " * 40, width=50)
        self.assertTrue(line.startswith(ui.chip(" ◎ /goal · step 1 ")))
        self.assertLess(visible_width(line), 50)
        self.assertTrue(plain(line).endswith("…"))
        self.assertEqual(plain(ui.goal_line(" ✓ done ", "short", width=80)), " ✓ done   short")

    def test_route_details_wrap_under_an_indent_instead_of_the_terminal_edge(self):
        import grokbot
        route = {"served": {"grok-4.7": 2}, "models": {"grok-bot-default": 2}, "calls": 2,
                 "usage": {"input": 92000, "cache_read": 200, "cache_write": 0, "output": 139}, "cents": 8.27}
        lines = grokbot.route_line(route, color=False, width=68).splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(len(line) < 68 for line in lines))
        self.assertTrue(lines[1].startswith("    ") and "8.27¢" in lines[1])
        self.assertEqual(len(grokbot.route_line(route, color=False).splitlines()), 1)   # piped: one line

    def test_footer_paints_the_goal_status(self):
        editor = ui.CursorInput([])
        editor.text, editor.notice, editor.working = "", None, None
        editor.hint = lambda: ("Grok Bot · Auto · Goal running · step 2", "Local · ask")
        self.assertIn(editor.fg("goal", "Goal running · step 2", True), editor.footer(100))
        # A narrow pane keeps the goal's state and drops the model label first.
        self.assertIn("Goal running · step 2", plain(editor.footer(44)))


if __name__ == "__main__":
    unittest.main()
