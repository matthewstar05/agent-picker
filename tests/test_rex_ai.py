import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import rex_ai


class FakeRex:
    """Records rex calls; answers the layout calls the way the server does."""

    def __init__(self, windows=None):
        self.calls, self.splits, self.typed, self.resizes = [], [], {}, []
        self.windows = windows or []
        self.count = 0

    def new_block(self):
        self.count += 1
        return f"block:{self.count}"

    rows = 70

    def __call__(self, *args, json_out=True, parse=False):
        self.calls.append(args)
        if args[:2] == ("block", "call") and args[-1] == "size":
            return {"rows": self.rows, "columns": 236, "desired": [{}]} if self.rows else {}
        if args[0] == "whoami":
            return {"block_id": "block:me", "window_id": "window:me"}
        if args[:2] == ("session", "inspect"):
            return {"windows": self.windows}
        if args[:2] == ("window", "new"):
            return {"window_id": "window:new", "block_ids": ["block:root"]}
        if args[0] == "new":
            return {"session_id": "session:new",
                    "initial_windows": [{"window_id": "window:first", "block_ids": ["block:root"]}]}
        if args[0] == "send":
            self.typed.setdefault(args[4], []).append(args[5].lstrip("\x15"))
        return {}

    def api(self, session, method, payload):
        if method == "session.resize_pane":
            self.resizes.append(payload)
            return {}
        self.splits.append(payload)
        count = 2 if payload["layout"]["split"]["direction"] == "vertical" else 4
        return {"block_ids": [self.new_block() for _ in range(count)]}


def windows(blocks):
    return [{"window_id": "window:me", "layers": [{"blocks": [{"block_id": b} for b in blocks]}]}]


class RexAiTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.state = tempfile.TemporaryDirectory()
        patcher = mock.patch.dict(os.environ, {"AGENT_PICKER_STATE_HOME": self.state.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.state.cleanup)

    def run_main(self, rex, argv=(), env=None, tty=False):
        env = {"REXAI_DRY": "0", **(env or {})}
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(rex_ai, "browser_wanted", return_value=False), \
                mock.patch.object(rex_ai, "spawn_fit") as self.spawned, \
                mock.patch.object(sys.stdin, "isatty", return_value=tty), \
                mock.patch.object(sys.stdout, "isatty", return_value=tty), \
                mock.patch("builtins.print"):
            for name in ("REX_SESSION", "REX_BLOCK"):
                if name not in (env or {}):
                    os.environ.pop(name, None)
            return rex_ai.main(list(argv), rex, "/work/proj")

    def test_builds_standard_with_equal_agent_columns(self):
        rex = FakeRex()
        self.assertEqual(self.run_main(rex), 0)
        side, grid = rex.splits
        self.assertEqual((side["anchor_id"], side["direction"], side["side"], side["ratio"]),
                         ("block:root", "horizontal", "after", 0.6667))
        self.assertEqual(side["layout"]["split"]["ratio"], 0.72)
        self.assertEqual((grid["direction"], grid["side"]), ("vertical", "before"))
        self.assertEqual(grid["ratio"], round(1 - (rex_ai.FS_ROWS + rex_ai.CHROME) / 74, 4))
        self.assertEqual(grid["layout"]["split"]["ratio"], 0.5)
        self.assertEqual(grid["layout"]["split"]["before"]["split"]["ratio"], 0.5)
        labels = [node["block"]["label"] for half in ("before", "after")
                  for node in grid["layout"]["split"][half]["split"].values() if isinstance(node, dict)]
        self.assertEqual(labels, ["Antigravity", "agent", "Codex", "Cursor/Kiro"])
        self.assertTrue(all(n["block"]["options"]["cwd"] == "/work/proj"
                            for n in side["layout"]["split"].values() if isinstance(n, dict)))

    def test_fs_keeps_its_rows_in_any_tab_height(self):
        for rows, want in ((44, 0.6875), (100, 0.8558)):
            rex = FakeRex()
            rex.rows = rows
            self.run_main(rex)
            self.assertEqual(rex.splits[1]["ratio"], want)

    def test_drawn_tab_gets_every_border_back_and_fs_its_rows(self):
        heights = iter([8, 8, 10, 11, 11, 11])

        class Drawn(FakeRex):
            def __call__(self, *args, json_out=True, parse=False):
                if args[:2] == ("block", "call") and args[5] == panes["terminal"]:
                    self.calls.append(args)
                    return {"rows": next(heights), "desired": [{}]}
                return super().__call__(*args, json_out=json_out, parse=parse)

        rex = Drawn()
        panes = {r: "block:" + r for r in ("agy", "picker", "codex", "cursor", "claude", "usage", "terminal")}
        ratio = rex_ai.fit_terminal(rex, "session:s", panes, 0.84, sleep=lambda _: None)
        self.assertLess(ratio, 0.84)
        # Autobalance evened the tab out: the side column and usage come back.
        side = [r for r in rex.resizes if r["after_block_id"] == "block:claude"]
        usage = [r for r in rex.resizes if r["after_block_id"] == "block:usage"]
        self.assertEqual({r["ratio"] for r in side}, {rex_ai.SIZES["side"]})
        self.assertEqual({r["ratio"] for r in usage}, {rex_ai.SIZES["usage"]})
        self.assertEqual(len(side), 2)   # once, then again for a late rebalance
        fs = [r for r in rex.resizes if r["after_block_id"] == "block:terminal"]
        self.assertEqual(fs[-1]["ratio"], ratio)

    def test_undrawn_tab_is_left_alone(self):
        rex = FakeRex()
        rex.rows = 0
        rex_ai.fit_terminal(rex, "session:s", {"agy": "a", "picker": "p", "terminal": "t"}, 0.85,
                            wait=0, sleep=lambda _: None)
        self.assertEqual(rex.resizes, [])

    def test_measured_chrome_is_remembered_for_the_next_layout(self):
        sizes = {"block:agy": 26, "block:picker": 26, "block:terminal": 11, "block:claude": 49, "block:usage": 17}

        class Drawn(FakeRex):
            def __call__(self, *args, json_out=True, parse=False):
                if args[:2] == ("block", "call") and args[-1] == "size":
                    return {"rows": sizes[args[5]], "desired": [{}]}
                return super().__call__(*args, json_out=json_out, parse=parse)

        panes = {r: "block:" + r for r in ("agy", "picker", "codex", "cursor", "claude", "usage", "terminal")}
        rex_ai.fit_terminal(Drawn(), "session:s", panes, 0.8, sleep=lambda _: None)
        self.assertEqual(rex_ai.chrome(), 3)

    def test_unknown_height_falls_back_to_a_fixed_share(self):
        rex = FakeRex()
        rex.rows = 0
        self.run_main(rex)
        self.assertEqual(rex.splits[1]["ratio"], rex_ai.SIZES["terminal"])

    def test_panes_run_the_picker_without_herdr_handoff(self):
        rex = FakeRex()
        self.run_main(rex)
        lines = [line for typed in rex.typed.values() for line in typed]
        self.assertEqual(len(lines), 6)
        self.assertTrue(any("--picker-launch Claude" in line for line in lines))
        self.assertTrue(any(line.endswith("--picker-usage") for line in lines))
        self.assertFalse(any("--picker-solo" in line for line in lines))

    def test_selectors_put_a_selector_in_every_agent_pane(self):
        rex = FakeRex()
        self.run_main(rex, ["--selectors"])
        lines = [line for typed in rex.typed.values() for line in typed]
        self.assertEqual(sum(line.endswith("--picker-return") for line in lines), 5)

    def test_outside_rex_opens_a_session(self):
        rex = FakeRex()
        self.run_main(rex, ["demo"])
        self.assertIn(("new", "demo", "-c", "/work/proj", "--window", "demo"), rex.calls)
        self.assertTrue(any(c[0] == "do" and c[1] == "session.select" for c in rex.calls))

    def test_busy_tab_gets_a_new_tab_built_out_of_sight(self):
        rex = FakeRex(windows(["block:me", "block:other"]))
        self.run_main(rex, env={"REX_SESSION": "session:s", "REX_BLOCK": "block:me"}, tty=True)
        self.assertIn(("window", "new", "-s", "session:s", "proj", "-c", "/work/proj", "--focus=false"), rex.calls)
        self.assertEqual(rex.splits[0]["anchor_id"], "block:root")
        # Shown only once it is finished, so Autobalance never sees panes open.
        self.assertEqual(rex.calls[-1], ("focus", "-s", "session:s", "window:new"))
        self.assertFalse(any(c[0] == "swap" for c in rex.calls))

    def test_single_pane_tab_moves_its_pane_into_the_finished_layout(self):
        rex = FakeRex(windows(["block:me"]))
        self.run_main(rex, env={"REX_SESSION": "session:s", "REX_BLOCK": "block:me"}, tty=True)
        order = [c[:2] for c in rex.calls]
        self.assertIn(("swap", "-s"), order)
        self.assertIn(("swap", "-s", "session:s", "block:me", "block:root"), rex.calls)
        self.assertIn(("window", "close", "-s", "session:s", "window:me"), rex.calls)
        self.assertLess(order.index(("swap", "-s")), order.index(("focus", "-s")))
        self.assertLess(order.index(("focus", "-s")), order.index(("window", "close")))
        self.assertEqual(self.spawned.call_args[0][2]["terminal"], "block:me")

    def test_fs_row_is_fitted_after_the_layout(self):
        rex = FakeRex()
        self.run_main(rex)
        self.spawned.assert_called_once()

    def test_dry_run_types_nothing(self):
        rex = FakeRex()
        self.run_main(rex, env={"REXAI_DRY": "1"})
        self.assertEqual(rex.typed, {})


if __name__ == "__main__":
    unittest.main()
