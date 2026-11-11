import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import bloom
import preferences
import themes

PINK, SKY = bloom.rgb("#f2a7c3"), bloom.rgb("#14121f")
BARK = (90, 63, 58)


class BloomTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.herdr = root / "herdr" / "config.toml"
        self.herdr.parent.mkdir()
        self.herdr.write_text('[theme]\nname = "vesper"\n\n[ui]\ncopy_on_select = false\n', encoding="utf-8")
        self.ghostty = root / "ghostty" / "config.ghostty"
        self.ghostty.parent.mkdir()
        self.ghostty.write_text("theme = 0x96f\n", encoding="utf-8")
        self.scenes = root / "scenes"
        bloom.write_png(self.scenes / "tiny.png", [[SKY, SKY, BARK, PINK], [SKY, PINK, BARK, BARK]])
        self.signals = []
        for target, value in ((bloom, {"SUPPORTED": True, "SCENES": self.scenes,
                                       "reload_ghostty": lambda: self.signals.append("ghostty")}),
                              (preferences, {"PATH": root / "preferences.json", "_values": None,
                                             "_stamp": None, "_checked": 0.0,
                                             "SETTINGS": list(preferences.SETTINGS)})):
            for name, replacement in value.items():
                self._patch(target, name, replacement)
        env = {"AGENT_PICKER_STATE_HOME": str(root / "state"), "HERDR_CONFIG_PATH": str(self.herdr),
               "AGENT_PICKER_GHOSTTY_CONFIG": str(self.ghostty)}
        patcher = patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        choices, default = list(bloom.SCENE.choices), bloom.SCENE.default
        self.addCleanup(lambda: (bloom.SCENE.choices.__setitem__(slice(None), choices),
                                 setattr(bloom.SCENE, "default", default)))
        bloom.register()
        self.calls = []

    def _patch(self, target, name, replacement):
        patcher = patch.object(target, name, replacement)
        patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, method, params):
        self.calls.append(method)
        return {"status": "ok"}

    def active(self):
        return bloom.active_path().read_text(encoding="utf-8")

    def image(self):
        line = next(l for l in self.active().splitlines() if l.startswith("background-image = "))
        return bloom.read_png(line.split(" = ", 1)[1])

    def test_png_round_trips_and_scales_each_art_pixel(self):
        path = Path(self.tmp.name) / "out.png"
        rows = [[(1, 2, 3), (250, 0, 9)], [(0, 0, 0), (255, 255, 255)]]
        bloom.write_png(path, rows)
        self.assertEqual(bloom.read_png(path), (2, 2, rows))
        bloom.write_png(path, rows, scale=3)
        width, height, big = bloom.read_png(path)
        self.assertEqual((width, height), (6, 6))
        self.assertEqual(big[2], [(1, 2, 3)] * 3 + [(250, 0, 9)] * 3)
        self.assertEqual(big[3], [(0, 0, 0)] * 3 + [(255, 255, 255)] * 3)

    def test_shipped_scenes_load_and_use_the_colors_that_follow_a_theme(self):
        names = [path.stem for path in (ROOT / "bloom" / "scenes").glob("*.png")]
        self.assertIn("elder-tree", names)
        for name in names:
            width, height, rows = bloom.read_png(ROOT / "bloom" / "scenes" / f"{name}.png")
            self.assertEqual((width, height, len(rows), len(rows[0])), (378, 246, 246, 378))
            used = {pixel for row in rows for pixel in row}
            self.assertTrue(used & {bloom.rgb(c) for c in bloom.GROUPS["blossom"]}, name)
            self.assertTrue(used & {bloom.rgb(c) for c in bloom.SKY}, name)

    def test_repaint_takes_theme_colors_and_leaves_the_rest(self):
        rows = [[SKY, BARK, PINK]]
        blue = [[30, 90, 200], [40, 180, 230], [120, 240, 230]]
        plain = bloom.repaint(rows, blue, tint=False)[0]
        self.assertEqual(plain, [SKY, BARK, (120, 240, 230)])      # blossoms take the last stop
        tinted = bloom.repaint(rows, blue, tint=True)[0]
        self.assertNotEqual(tinted[0], SKY)
        self.assertEqual(max(tinted[0]), max(SKY))                 # same brightness, new hue
        self.assertEqual(tinted[0].index(max(tinted[0])), 2)       # leaning blue
        self.assertEqual(bloom.repaint(rows, None), rows)          # brand keeps the drawn colors
        self.assertEqual(bloom.repaint(rows, "rainbow")[0][:2], [SKY, BARK])
        self.assertNotEqual(bloom.repaint(rows, "rainbow")[0][2], PINK)

    def test_shift_slides_toward_the_tree_and_mirror_flips(self):
        a, b, c, d = (1, 1, 1), (2, 2, 2), (3, 3, 3), (4, 4, 4)
        self.assertEqual(bloom.repaint([[a, b, c, d]], None, shift=1), [[a, a, b, c]])
        self.assertEqual(bloom.repaint([[a, b, c, d]], None, shift=2), [[b, a, a, b]])
        self.assertEqual(bloom.repaint([[a, b, c, d]], None, mirror=True), [[d, c, b, a]])
        self.assertEqual(bloom.repaint([[a, b, c, d]], None, mirror=True, shift=1), [[c, b, a, a]])

    def test_settings_live_on_their_own_page(self):
        page = preferences.find("bloom")
        self.assertIsInstance(page, preferences.Page)
        self.assertEqual([s.key for s in page.children], [s.key for s in bloom.SETTINGS])
        self.assertEqual(page.show(), "off")
        preferences.put(bloom.ENABLED.key, True)
        self.assertEqual(page.show(), "on · tiny")
        self.assertEqual(bloom.SCENE.choices, ["tiny"])

    def test_nothing_is_touched_while_off(self):
        before = (self.herdr.read_text(), self.ghostty.read_text())
        self.assertFalse(bloom.sync(self.call))
        self.assertEqual((self.herdr.read_text(), self.ghostty.read_text()), before)
        self.assertFalse(bloom.active_path().exists())
        self.assertEqual((self.calls, self.signals), ([], []))

    def test_on_writes_both_configs_once_and_off_restores_them(self):
        herdr_before, ghostty_before = self.herdr.read_text(), self.ghostty.read_text()
        preferences.put(bloom.ENABLED.key, True)
        self.assertTrue(bloom.sync(self.call))
        herdr = self.herdr.read_text()
        self.assertIn('[theme.custom]\npanel_bg = "reset"\nsidebar_bg = "reset"', herdr)
        self.assertTrue(herdr.startswith(herdr_before))
        self.assertEqual(self.ghostty.read_text().splitlines()[-1], bloom.include_line())
        self.assertIn("background-image-opacity = 0.45", self.active())
        self.assertIn("background-image-position = bottom-right", self.active())
        self.assertEqual(self.image()[:2], (4 * bloom.SCALE, 2 * bloom.SCALE))
        self.assertEqual((self.calls, self.signals), (["server.reload_config"], ["ghostty"]))

        self.assertFalse(bloom.sync(self.call))                    # a second look changes nothing
        self.assertEqual((self.herdr.read_text(), self.calls, self.signals),
                         (herdr, ["server.reload_config"], ["ghostty"]))

        preferences.put(bloom.ENABLED.key, False)
        self.assertTrue(bloom.sync(self.call))
        self.assertEqual(self.herdr.read_text(), herdr_before)
        self.assertFalse(bloom.active_path().exists())
        self.assertEqual(self.ghostty.read_text().splitlines()[0], ghostty_before.strip())   # the include stays, harmless
        self.assertFalse(bloom.sync(self.call))

    def test_settings_and_the_usage_theme_repaint_the_backdrop(self):
        preferences.put(bloom.ENABLED.key, True)
        self.assertEqual(preferences.get("usage.theme") or "brand", "brand")
        bloom.sync(self.call)
        self.assertEqual(self.image()[2][0][-1], PINK)             # brand: as drawn
        self.assertIn("background = #14121f", self.active())

        ocean = themes.stops("ocean")
        with patch.object(themes, "stops", lambda name: ocean):
            self.assertTrue(bloom.sync(self.call))
            self.assertEqual(self.image()[2][0][-1], tuple(ocean[-1]))
            self.assertNotIn("background = #14121f", self.active())   # the tint follows the theme
            self.assertIn(f'accent = "{bloom.hexa(ocean[-1])}"', self.herdr.read_text())
            preferences.put(bloom.TINT.key, False)
            self.assertTrue(bloom.sync(self.call))
            self.assertIn("background = #14121f", self.active())
            preferences.put(bloom.FOLLOW.key, False)
            self.assertTrue(bloom.sync(self.call))
            self.assertEqual(self.image()[2][0][-1], PINK)

        preferences.put(bloom.SIDE.key, "left")
        preferences.put(bloom.DIM.key, 25)
        self.assertTrue(bloom.sync(self.call))
        self.assertIn("background-image-position = bottom-left", self.active())
        self.assertIn("background-image-opacity = 0.25", self.active())
        self.assertEqual(self.image()[2][0][0], PINK)              # mirrored

    def test_your_own_theme_custom_block_is_left_alone(self):
        self.herdr.write_text(self.herdr.read_text() + '\n[theme.custom]\naccent = "#ff8800"\n', encoding="utf-8")
        before = self.herdr.read_text()
        preferences.put(bloom.ENABLED.key, True)
        self.assertTrue(bloom.sync(self.call))
        self.assertEqual(self.herdr.read_text(), before)
        self.assertTrue(bloom.active_path().exists())
        self.assertFalse(bloom.sync(self.call))

    def test_a_config_herdr_refuses_is_put_back(self):
        before = self.herdr.read_text()
        preferences.put(bloom.ENABLED.key, True)
        bloom.sync(lambda method, params: {"status": "failed"})
        self.assertEqual(self.herdr.read_text(), before)

    def test_without_ghostty_or_on_windows_the_setting_waits(self):
        preferences.put(bloom.ENABLED.key, True)
        before = self.herdr.read_text()
        with patch.object(bloom, "ghostty_config_path", lambda: None):
            self.assertFalse(bloom.sync(self.call))
        with patch.object(bloom, "SUPPORTED", False):
            self.assertFalse(bloom.sync(self.call))
            self.assertIsNone(bloom.register())
            self.assertIn("not available", bloom.status()[0])
        self.assertEqual(self.herdr.read_text(), before)

    def test_command_switches_the_setting(self):
        with patch("builtins.print"):
            self.assertEqual(bloom.command(["on"]), 0)
            self.assertTrue(preferences.get(bloom.ENABLED.key))
            self.assertTrue(bloom.active_path().exists())
            self.assertEqual(bloom.command(["toggle"]), 0)
            self.assertFalse(bloom.active_path().exists())
            self.assertEqual(bloom.command(["status"]), 0)
            self.assertEqual(bloom.command(["sideways"]), 2)


if __name__ == "__main__":
    unittest.main()
