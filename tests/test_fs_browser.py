import os
import re
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import fs_browser as fsb
import preferences

ANSI = re.compile(r"\x1b\[[0-9;?<>]*[A-Za-z]|\x1b\][^\x07]*\x07")


class FakeTerminal:
    def __init__(self):
        self.out, self.shift, self.mice = [], None, []

    def write(self, text):
        self.out.append(text)

    def mouse(self, on):
        self.mice.append(on)


def screen_text(frame, cols, rows):
    """Replay segment rows into plain text, checking nothing spills past the right edge."""
    grid = [[" "] * cols for _ in range(rows)]
    for y, row in enumerate(frame):
        for col, text in row:
            plain = ANSI.sub("", text)
            x = col - 1
            for ch in plain:
                w = fsb.char_width(ch)
                if w == 0:
                    continue
                assert x + w <= cols, f"row {y} spills past column {cols}: {plain!r}"
                grid[y][x] = ch
                if w == 2:
                    grid[y][x + 1] = ""
                x += w
    return ["".join(row) for row in grid]


class Base(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        for patcher in (patch.object(preferences, "PATH", self.root / "preferences.json"),
                        patch.object(preferences, "_values", {}),
                        patch.dict(os.environ, {"AGENT_PICKER_STATE_HOME": str(self.root / "state")})):
            patcher.start()
            self.addCleanup(patcher.stop)
        for key in ("HERDR_ENV", "HERDR_PANE_ID"):
            if key in os.environ:
                patcher = patch.dict(os.environ)
                patcher.start()
                self.addCleanup(patcher.stop)
                os.environ.pop(key)
        self.tree = self.root / "tree"
        (self.tree / "alpha").mkdir(parents=True)
        (self.tree / "beta").mkdir()
        (self.tree / "alpha" / "inside.txt").write_text("inside\n")
        (self.tree / "notes.md").write_text("# Notes\n\nhello\n")
        (self.tree / "file2.txt").write_text("2")
        (self.tree / "file10.txt").write_text("10")
        (self.tree / "data.bin").write_bytes(b"\x00\x01\x02" * 100)
        (self.tree / ".secret").write_text("x")

    def app(self, **kwargs):
        app = fsb.App(self.tree, **kwargs)
        app.terminal = FakeTerminal()
        return app

    def draw(self, app, cols=120, rows=12):
        writer = fsb.SegmentScreen(app.terminal)
        frame = app.frame(cols, rows, 1.0)
        writer.show(frame, cols, rows)
        return screen_text(frame, cols, rows)

    def names(self, app):
        return [e.name for e in app.entries]


class TextTests(unittest.TestCase):
    def test_middle_keeps_the_extension_and_the_width(self):
        text = fsb.middle("a_really_long_file_name_for_testing.json", 16)
        self.assertEqual(fsb.text_width(text), 16)
        self.assertTrue(text.endswith(".json"))
        self.assertIn("…", text)
        self.assertEqual(fsb.middle("short.py", 16), "short.py")

    def test_wide_characters_count_two_columns(self):
        self.assertEqual(fsb.text_width("日本語.txt"), 10)
        cut = fsb.fit("日本語のファイル", 7)
        self.assertLessEqual(fsb.text_width(cut), 7)
        cells = fsb.cells_of("日a")
        self.assertEqual([c[0] for c in cells], ["日", "", "a"])

    def test_control_characters_in_names_stay_on_one_row(self):
        self.assertEqual(fsb.clean("a\nb\tc"), "a?b?c")

    def test_tile_names_break_at_a_separator_then_shorten_in_the_middle(self):
        self.assertEqual(fsb.tile_name("docs", 14, 2), ["docs"])
        lines = fsb.tile_name("Install agent-picker.command", 14, 2)
        self.assertEqual(lines[0], "Install agent-")
        self.assertEqual(lines[1], "picker.command")
        lines = fsb.tile_name("Screenshot 2026-11-09 at 12.04.05 PM.png", 14, 2)
        self.assertTrue(all(fsb.text_width(line) <= 14 for line in lines))
        self.assertTrue(lines[1].endswith(".png"))
        self.assertEqual(len(fsb.tile_name("x" * 40, 14, 1)), 1)
        self.assertEqual(fsb.tile_name("GameDev Projects", 14, 2), ["GameDev", "Projects"])

    def test_sizes_read_like_finder(self):
        self.assertEqual(fsb.human_size(1), "1 byte")
        self.assertEqual(fsb.human_size(289), "289 bytes")
        self.assertEqual(fsb.human_size(1500), "2 KB")
        self.assertEqual(fsb.human_size(1_200_000), "1.2 MB")
        self.assertEqual(fsb.human_size(340_000_000_000), "340 GB")

    def test_dates_shorten_to_fit(self):
        now = datetime(2026, 11, 9, 15, 0)
        today = datetime(2026, 11, 9, 9, 5).timestamp()
        self.assertEqual(fsb.when(today, 24, now), "Today at 9:05 AM")
        self.assertEqual(fsb.when(today, 8, now), "9:05 AM")
        yesterday = datetime(2026, 11, 8, 21, 30).timestamp()
        self.assertEqual(fsb.when(yesterday, 24, now), "Yesterday at 9:30 PM")
        self.assertEqual(fsb.when(yesterday, 13, now), "Yesterday")
        old = datetime(2026, 10, 3, 10, 1).timestamp()
        self.assertEqual(fsb.when(old, 24, now), "Oct 3, 2026 at 10:01 AM")
        self.assertEqual(fsb.when(old, 13, now), "Oct 3, 2026")
        self.assertEqual(fsb.when(old, 8, now), "10/3/26")

    def test_paths_shorten_from_the_left(self):
        path = Path.home() / "Desktop" / "Projects" / "agent-picker"
        self.assertEqual(fsb.tail_path(path, 80), os.path.join("~", "Desktop", "Projects", "agent-picker"))
        short = fsb.tail_path(path, 18)
        self.assertTrue(short.startswith("…") and short.endswith("agent-picker"))
        self.assertLessEqual(fsb.text_width(short), 18)


class FolderTests(Base):
    def test_scan_reads_kinds_hidden_items_and_links(self):
        if hasattr(os, "symlink") and os.name != "nt":
            os.symlink(self.tree / "notes.md", self.tree / "link.md")
            os.symlink(self.tree / "gone", self.tree / "broken")
        entries, error = fsb.scan(self.tree)
        self.assertIsNone(error)
        by = {e.name: e for e in entries}
        self.assertTrue(by["alpha"].is_dir and by["alpha"].opens)
        self.assertTrue(by[".secret"].hidden)
        self.assertEqual(by["notes.md"].kind, "markdown")
        if "link.md" in by:
            self.assertTrue(by["link.md"].is_link)
            self.assertIn("alias", by["link.md"].kind_text())
            self.assertTrue(by["broken"].broken)

    def test_an_unreadable_folder_reports_why(self):
        entries, error = fsb.scan(self.tree / "missing")
        self.assertEqual(entries, [])
        self.assertTrue(error)

    def test_sorting_is_natural_and_keeps_folders_on_top(self):
        entries, _ = fsb.scan(self.tree)
        visible = [e for e in entries if not e.hidden]
        names = [e.name for e in fsb.arrange(visible, "name", True)]
        self.assertEqual(names[:2], ["alpha", "beta"])
        self.assertLess(names.index("file2.txt"), names.index("file10.txt"))
        mixed = [e.name for e in fsb.arrange(visible, "name", False)]
        self.assertLess(mixed.index("data.bin"), mixed.index("file2.txt"))
        os.utime(self.tree / "data.bin", (2_000_000_000, 2_000_000_000))
        entries, _ = fsb.scan(self.tree)
        newest = fsb.arrange([e for e in entries if not e.is_dir], "date", False)
        self.assertEqual(newest[0].name, "data.bin")
        largest = fsb.arrange([e for e in entries if not e.hidden], "size", False)
        self.assertEqual(largest[0].name, "data.bin")
        self.assertTrue(all(e.is_dir for e in largest[-2:]))


class NavigationTests(Base):
    def test_opens_folders_and_remembers_where_it_came_from(self):
        app = self.app()
        self.assertEqual(self.names(app)[:2], ["alpha", "beta"])
        self.assertNotIn(".secret", self.names(app))
        app.handle("\r")
        self.assertEqual(app.cwd, self.tree / "alpha")
        self.assertEqual(self.names(app), ["inside.txt"])
        app.handle("\x7f")
        self.assertEqual(app.cwd, self.tree)
        self.assertEqual(app.selected().name, "alpha")
        app.handle("[")
        self.assertEqual(app.cwd, self.tree / "alpha")
        app.handle("]")
        self.assertEqual(app.cwd, self.tree)

    def test_only_shift_q_quits(self):
        app = self.app()
        for key in ("q", "\x03", "\x04"):
            self.assertIsNone(app.handle(key), key)
        self.assertEqual(app.handle("Q"), "quit")
        for screen_key in ("t", "s", "?"):
            app = self.app()
            app.handle(screen_key)
            self.assertEqual(app.handle("Q"), "quit", screen_key)

    def test_esc_goes_back_a_folder_after_clearing_a_filter(self):
        app = self.app()
        app.handle("\r")
        self.assertEqual(app.cwd, self.tree / "alpha")
        app.handle("esc")
        self.assertEqual(app.cwd, self.tree)
        self.assertEqual(app.selected().name, "alpha")
        for key in "/fi":
            app.handle(key)
        app.handle("\r")
        app.handle("esc")
        self.assertEqual((app.cwd, app.query), (self.tree, None))
        app.handle("esc")
        self.assertEqual(app.cwd, self.root)
        app.go(Path(app.cwd.anchor))
        app.handle("esc")   # nothing above the root
        self.assertEqual(app.cwd, Path(app.cwd.anchor))

    def test_esc_in_a_preview_returns_to_the_folder(self):
        app = self.app()
        app.index = self.names(app).index("notes.md")
        app.handle("\r")
        app.handle("q")
        self.assertEqual(app.screen, "preview")
        app.handle("esc")
        self.assertEqual((app.screen, app.cwd), ("browse", self.tree))

    def test_arrows_move_across_the_grid_then_down_a_row(self):
        app = self.app()
        self.draw(app, 40, 9)   # a grid two tiles wide
        self.assertEqual(app.ncols, 2)
        app.handle("right")
        self.assertEqual(app.index, 1)
        app.handle("down")
        self.assertEqual(app.index, 3)
        app.handle("up")
        self.assertEqual(app.index, 1)
        app.handle("left")
        app.handle("left")
        self.assertEqual(app.index, 0)

    def test_hidden_files_view_and_sort_are_saved_settings(self):
        app = self.app()
        app.handle(".")
        self.assertIn(".secret", self.names(app))
        self.assertTrue(preferences.get(fsb.HIDDEN.key))
        app.handle("v")
        self.assertEqual(preferences.get(fsb.VIEW.key), "details")
        app.handle(",")
        self.assertEqual(preferences.get(fsb.SORT.key), "kind")

    def test_command_line_choices_last_only_this_run(self):
        app = self.app(view="details", hidden=True, sort="size")
        self.assertIn(".secret", self.names(app))
        app.handle("v")
        self.assertEqual(app.view, "icons")
        self.assertEqual(preferences.get(fsb.VIEW.key), "icons")   # the saved default is untouched
        self.assertFalse(os.path.exists(preferences.PATH) and "fs.view" in preferences.PATH.read_text())

    def test_filtering_narrows_by_name_and_esc_clears(self):
        app = self.app()
        for key in "/file":
            app.handle(key)
        self.assertEqual(self.names(app), ["file2.txt", "file10.txt"])
        app.handle("\r")
        self.assertFalse(app.typing)
        self.assertEqual(len(app.entries), 2)
        app.handle("esc")
        self.assertIsNone(app.query)
        self.assertIn("alpha", self.names(app))

    def test_a_file_on_the_command_line_opens_its_folder_selected(self):
        with patch.object(fsb.App, "run", lambda self: self.selected().name), \
                patch.object(sys.stdin, "isatty", lambda: True), patch.object(sys.stdout, "isatty", lambda: True):
            self.assertEqual(fsb.main([str(self.tree / "notes.md")]), "notes.md")
            self.assertEqual(fsb.main([str(self.tree / "nowhere")]), 2)

    def test_mouse_clicks_select_and_double_clicks_open(self):
        app = self.app()
        self.draw(app, 120, 9)
        x, y, w, h, i = next(hit for hit in app.hits if hit[4] == 1)
        app.handle(f"mouse:0:{x + 2}:{y + 1}:press")
        self.assertEqual(app.index, 1)
        app.handle(f"mouse:0:{x + 2}:{y + 1}:press")
        self.assertEqual(app.cwd, self.tree / "beta")

    def test_the_folder_is_watched_for_changes(self):
        app = self.app()
        (self.tree / "new.txt").write_text("new")
        os.utime(self.tree, (2_000_000_000, 2_000_000_000))
        app.checked = time.monotonic() - 5
        app.watch()
        self.assertIn("new.txt", self.names(app))


class DrawingTests(Base):
    def test_every_pane_size_draws_inside_its_bounds(self):
        for view in ("icons", "details"):
            app = self.app(view=view)
            for cols, rows in ((179, 9), (179, 11), (120, 30), (60, 12), (40, 7), (30, 5), (24, 4), (18, 3)):
                lines = self.draw(app, cols, rows)
                self.assertEqual(len(lines), rows)
                self.assertTrue(any("alpha" in line for line in lines), (view, cols, rows, lines))

    def test_herdr_pane_heights_show_one_row_of_large_icons(self):
        grid = fsb.Grid.choose(177, 7, 30)
        self.assertEqual((grid.size, grid.rows_vis, grid.lines), ("large", 1, 2))
        self.assertGreaterEqual(grid.ncols, 10)
        grid = fsb.Grid.choose(177, 12, 30)
        self.assertEqual((grid.size, grid.rows_vis), ("large", 2))
        self.assertEqual(fsb.Grid.choose(177, 5, 30).size, "small")
        self.assertEqual(fsb.Grid.choose(177, 3, 30).size, "mini")
        self.assertFalse(fsb.Grid.choose(177, 1, 30).lids)

    def test_a_one_line_name_sits_a_row_below_its_icon(self):
        app = self.app()
        grid = fsb.Grid.choose(177, 7, 6)
        short = app.tile(app.entries[0], grid, None)              # "alpha"
        blank = lambda row: all(c[0] == " " for c in row)
        icon_rows, name_row = short[1:4], short[5]
        self.assertTrue(all(not blank(row) for row in icon_rows))
        self.assertTrue(blank(short[4]))
        self.assertIn("alpha", "".join(c[0] for c in name_row))
        long_name = fsb.Entry("Install agent-picker.command", self.tree / "x", kind="executable")
        tall = app.tile(long_name, grid, None)
        self.assertIn("Install agent-", "".join(c[0] for c in tall[4]))
        self.assertIn("picker.command", "".join(c[0] for c in tall[5]))

    def test_the_frame_names_the_folder_and_its_keys(self):
        lines = self.draw(self.app(), 179, 9)
        self.assertIn("tree", lines[0])
        self.assertIn("6 items", lines[-1])
        self.assertIn("⇧⏎", lines[-1])

    def test_only_the_selected_tile_and_border_redraw_between_frames(self):
        app = self.app()
        writer = fsb.SegmentScreen(app.terminal)
        writer.show(app.frame(179, 9, 1.0), 179, 9)
        app.terminal.out = []
        writer.show(app.frame(179, 9, 1.5), 179, 9)
        sent = "".join(app.terminal.out)
        self.assertNotIn("notes.md", ANSI.sub("", sent))
        self.assertNotIn("\x1b[2J", sent)

    def test_holding_shift_says_where_the_agents_go(self):
        app = self.app()
        app.terminal.shift = True
        app.handle("shift")
        with patch.dict(os.environ, {"HERDR_ENV": "1", "HERDR_PANE_ID": "w1:p1"}):
            lines = self.draw(app, 179, 9)
        self.assertIn("moves this tab's agents to alpha", lines[-1])
        app.handle("release:shift")
        app.terminal.shift = False
        lines = self.draw(app, 179, 9)
        self.assertNotIn("moves this tab's agents", lines[-1])

    def test_losing_focus_holds_the_highlight_still(self):
        app = self.app()
        app.handle("focus-out")
        self.assertFalse(app.animating())
        self.assertEqual(app.frame_step(), 1.0)
        app.handle("focus-in")
        self.assertTrue(app.animating())


class RouteTests(Base):
    def run_route(self, code, output, inside=True):
        app = self.app()
        calls = []

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            return SimpleNamespace(returncode=code, stdout=output, stderr="")

        env = {"HERDR_ENV": "1", "HERDR_PANE_ID": "w1:p1"} if inside else {}
        with patch.dict(os.environ, env), patch.object(fsb.subprocess, "run", fake_run):
            app.handle("shift-enter")
            deadline = time.monotonic() + 5
            while app.routing and app.routing["result"] is None and time.monotonic() < deadline:
                time.sleep(0.01)
            app.routed()
        return app, calls

    def test_shift_enter_refreshes_the_tab_into_the_selected_folder(self):
        app, calls = self.run_route(0, "herdr-ai: kept codex (w1:p5) running: working\n"
                                       "herdr-ai: refreshed claude, agy, cursor in /x; conversations preserved\n")
        argv, kwargs = calls[0]
        self.assertEqual(argv[1:], [str(fsb.HERDR_AI), "--refresh"])
        self.assertEqual(kwargs["cwd"], str(self.tree / "alpha"))
        self.assertEqual(app.cwd, self.tree / "alpha")
        self.assertEqual(app.toast[0], "✓ 3 agents now in alpha · 1 kept running")

    def test_a_file_routes_to_its_folder(self):
        app = self.app()
        app.index = self.names(app).index("notes.md")
        self.assertEqual(app.route_target(), self.tree)

    def test_a_tab_without_a_layout_offers_herdr_ai(self):
        app, _ = self.run_route(3, "herdr-ai: this tab has no herdr-ai layout\n")
        self.assertEqual(app.screen, "confirm")
        self.assertEqual(app.confirm.kind, "layout")
        with patch.object(fsb.App, "run_foreground", lambda self, argv, cwd: self.ran.append((argv, cwd)) or 0):
            app.ran = []
            app.handle("\r")
        self.assertEqual(app.ran[0][0][1:], [str(fsb.HERDR_AI), "--no-fs"])
        self.assertEqual(app.ran[0][1], self.tree / "alpha")

    def test_failures_are_reported(self):
        app, _ = self.run_route(1, "herdr-ai: this tab's Codex lead pane is gone; run herdr-reset\n")
        self.assertEqual(app.cwd, self.tree)
        self.assertIn("lead pane is gone", app.toast[0])

    def test_outside_herdr_it_asks_before_opening_a_workspace(self):
        app, calls = self.run_route(0, "", inside=False)
        self.assertEqual(calls, [])
        self.assertEqual((app.screen, app.confirm.kind), ("confirm", "herdr"))
        app.handle("esc")
        self.assertEqual(app.screen, "browse")


class ScreenTests(Base):
    def test_the_theme_menu_previews_then_applies_or_reverts(self):
        app = self.app()
        app.handle("t")
        self.assertEqual(app.screen, "themes")
        app.handle("down")
        app.handle("down")
        self.assertEqual(app.theme(), "aurora")
        self.draw(app, 120, 16)
        app.handle("esc")
        self.assertEqual(app.screen, "browse")
        self.assertEqual(preferences.get(fsb.THEME.key), "usage")
        app.handle("t")
        app.handle("down")
        app.handle("down")
        app.handle("\r")
        self.assertEqual(preferences.get(fsb.THEME.key), "aurora")
        self.assertEqual(app.theme(), "aurora")

    def test_the_default_theme_follows_the_usage_panel(self):
        app = self.app()
        preferences.put("usage.theme", "ember")
        self.assertEqual(app.theme(), "ember")
        self.assertEqual(fsb.App(self.tree, theme="vapor").theme(), "vapor")

    def test_settings_and_help_open_over_the_browser(self):
        app = self.app()
        app.handle("s")
        self.assertEqual(app.screen, "settings")
        lines = self.draw(app, 120, 24)
        self.assertTrue(any("FILE BROWSER" in line for line in lines))
        app.handle("esc")
        self.assertEqual(app.screen, "browse")
        app.handle("?")
        self.assertTrue(any("Quick Look" in line for line in self.draw(app, 120, 30)))
        app.handle("x")
        self.assertEqual(app.screen, "browse")

    def test_enter_previews_text_and_esc_returns(self):
        app = self.app()
        app.index = self.names(app).index("notes.md")
        app.handle("\r")
        self.assertEqual((app.screen, app.preview.mode), ("preview", "text"))
        lines = self.draw(app, 100, 10)
        self.assertIn("notes.md", lines[0])
        self.assertTrue(any("# Notes" in line for line in lines))
        app.handle("esc")
        self.assertEqual(app.screen, "browse")

    def test_binary_files_preview_their_details(self):
        app = self.app()
        app.index = self.names(app).index("data.bin")
        with patch.object(fsb.sys, "platform", "linux"):
            app.handle("\r")
        self.assertEqual(app.preview.mode, "info")
        self.assertTrue(any("300 bytes" in line for line in self.draw(app, 100, 12)))

    def test_pinning_makes_the_herdr_pane_open_here(self):
        app = self.app()
        app.handle("p")
        self.assertEqual(preferences.get(fsb.OPENS_IN.key), "pinned")
        self.assertEqual(fsb.herdr_start(self.root), self.tree)

    def test_the_herdr_pane_opens_where_the_setting_says(self):
        with patch.object(fsb, "desktop", lambda: self.tree / "alpha"):
            self.assertEqual(fsb.herdr_start(self.root), self.tree / "alpha")
            preferences.put(fsb.OPENS_IN.key, "herdr")
            self.assertEqual(fsb.herdr_start(self.tree), self.tree)
            preferences.put(fsb.OPENS_IN.key, "pinned")
            preferences.put(fsb.PINNED, str(self.root / "deleted"))
            self.assertEqual(fsb.herdr_start(self.tree), Path.home())


class ParseTests(unittest.TestCase):
    def test_options(self):
        opts = fsb.parse(["~/x", "--view", "icon", "--hidden", "--sort=date", "--herdr"])
        self.assertEqual((opts["path"], opts["view"], opts["hidden"], opts["sort"], opts["herdr"]),
                         ("~/x", "icons", True, "date", True))
        self.assertEqual(fsb.parse(["--details"])["view"], "details")
        self.assertEqual(fsb.parse(["--theme", "AURORA"])["theme"], "aurora")
        self.assertEqual(fsb.parse(["--", "-odd-name"])["path"], "-odd-name")

    def test_errors(self):
        self.assertIn("unknown option", fsb.parse(["--nope"]))
        self.assertIn("--view takes", fsb.parse(["--view", "tiles"]))
        self.assertIn("no theme named", fsb.parse(["--theme", "nope"]))
        self.assertIn("one folder", fsb.parse(["a", "b"]))

    def test_without_a_terminal_it_explains(self):
        with patch.object(sys.stdin, "isatty", lambda: False):
            self.assertEqual(fsb.main([]), 2)


class PictureTests(unittest.TestCase):
    def bmp(self, pixels, bpp=24, top_down=False):
        height, width = len(pixels), len(pixels[0])
        stride = (width * bpp + 31) // 32 * 4
        rows = []
        for row in (pixels if top_down else reversed(pixels)):
            data = b"".join(bytes((b, g, r)) if bpp == 24 else bytes((b, g, r, a)) for r, g, b, a in row)
            rows.append(data + b"\x00" * (stride - len(data)))
        body = b"".join(rows)
        header = (b"BM" + (54 + len(body)).to_bytes(4, "little") + b"\x00" * 4 + (54).to_bytes(4, "little")
                  + (40).to_bytes(4, "little") + width.to_bytes(4, "little")
                  + (-height if top_down else height).to_bytes(4, "little", signed=True)
                  + (1).to_bytes(2, "little") + bpp.to_bytes(2, "little") + b"\x00" * 24)
        return header + body

    def test_reads_24_and_32_bit_bitmaps_either_way_up(self):
        red, blue = (255, 0, 0, 255), (0, 0, 255, 255)
        for bpp, top_down in ((24, False), (32, True)):
            width, height, pixel = fsb.read_bmp(self.bmp([[red, blue], [blue, red]], bpp, top_down))
            self.assertEqual((width, height), (2, 2))
            self.assertEqual(pixel(0, 0)[:3], (255, 0, 0))
            self.assertEqual(pixel(1, 0)[:3], (0, 0, 255))
        self.assertIsNone(fsb.read_bmp(b"not a bitmap"))

    def test_zero_alpha_everywhere_means_opaque(self):
        image = fsb.read_bmp(self.bmp([[(10, 20, 30, 0)] * 2] * 2, 32))
        self.assertEqual(image[2](1, 1), (10, 20, 30, 255))

    def test_pictures_fit_the_cells_two_pixels_each(self):
        image = fsb.read_bmp(self.bmp([[(255, 255, 255, 255)] * 4] * 2))
        rows = fsb.picture_rows(image, 10, 5)
        self.assertEqual((len(rows), len(rows[0])), (5, 10))
        self.assertTrue(any(cell[0] in "▀▄" for row in rows for cell in row))


class WriterTests(unittest.TestCase):
    def test_unchanged_segments_are_not_resent(self):
        term = FakeTerminal()
        writer = fsb.SegmentScreen(term)
        writer.show([[(1, "aaa"), (4, "bbb")]], 10, 1)
        term.out = []
        writer.show([[(1, "aaa"), (4, "ccc")]], 10, 1)
        self.assertEqual("".join(term.out), "\x1b[?2026h\x1b[1;4Hccc\x1b[?2026l")
        term.out = []
        writer.show([[(1, "aaa"), (4, "ccc")]], 10, 1)
        self.assertEqual(term.out, [])


if __name__ == "__main__":
    unittest.main()
