import os
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import fs_icons


class ClassifyTests(unittest.TestCase):
    def test_files_by_extension_and_name(self):
        home = Path("/home/u")
        cases = {
            "a.py": "python", "A.PY": "python", "x.tsx": "typescript", "y.rs": "rust", "z.md": "markdown",
            "p.png": "image", "m.mov": "video", "s.mp3": "audio", "d.pdf": "pdf", "t.zip": "archive",
            "v.dmg": "diskimage", "c.json": "json", "c.yml": "yaml", "Makefile": "makefile",
            "Dockerfile": "makefile", "LICENSE": "license", "package-lock.json": "lock", ".gitignore": "config",
            ".zshrc": "config", "notes.txt": "text", "run.sh": "shell", "mystery.qqq": "file",
        }
        for name, kind in cases.items():
            self.assertEqual(fs_icons.classify(f"/w/{name}", home=home), kind, name)
        self.assertEqual(fs_icons.classify("/w/tool", executable=True, home=home), "executable")

    def test_folders_bundles_and_special_places(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            for name in ("Desktop", "Downloads", "repo/.git", "plain", "Thing.app"):
                (home / name).mkdir(parents=True)
            self.assertEqual(fs_icons.classify(home, is_dir=True, home=home), "home")
            self.assertEqual(fs_icons.classify(home / "Desktop", is_dir=True, home=home), "folder-desktop")
            self.assertEqual(fs_icons.classify(home / "Downloads", is_dir=True, home=home), "folder-downloads")
            self.assertEqual(fs_icons.classify(home / "repo", is_dir=True, home=home), "folder-git")
            self.assertEqual(fs_icons.classify(home / "plain", is_dir=True, home=home), "folder")
            self.assertEqual(fs_icons.classify(home / "Thing.app", is_dir=True, home=home), "app")
            # a Desktop folder somewhere else is an ordinary folder
            (home / "plain" / "Desktop").mkdir()
            self.assertEqual(fs_icons.classify(home / "plain" / "Desktop", is_dir=True, home=home), "folder")

    def test_finder_style_labels(self):
        self.assertEqual(fs_icons.label("folder"), "Folder")
        self.assertEqual(fs_icons.label("python", "a.py"), "Python script")
        self.assertEqual(fs_icons.label("image", "a.png"), "PNG image")
        self.assertEqual(fs_icons.label("file", "a.qqq"), "QQQ file")
        self.assertEqual(fs_icons.label("file", "README"), "Document")


class IconTests(unittest.TestCase):
    def test_every_kind_and_size_has_exact_dimensions_and_safe_cells(self):
        for kind in fs_icons.KINDS:
            for size, (cols, rows) in fs_icons.SIZES.items():
                for link in (False, True):
                    icon = fs_icons.icon(kind, size, link=link)
                    self.assertEqual(len(icon), rows, (kind, size))
                    for row in icon:
                        self.assertEqual(len(row), cols, (kind, size))
                        for ch, fg, bg, bold in row:
                            self.assertEqual(len(ch), 1, (kind, size, ch))
                            self.assertNotIn(unicodedata.east_asian_width(ch), ("W", "F"), (kind, ch))
                            for color in (fg, bg):
                                self.assertTrue(color is None or (len(color) == 3 and
                                                                  all(0 <= c <= 255 for c in color)))

    def test_outside_the_shape_is_transparent(self):
        page = fs_icons.icon("python", "large")
        self.assertIsNone(page[0][0][2])   # the browser's glow shows around a document
        self.assertEqual(page[0][0][0], " ")
        folder = fs_icons.icon("folder", "large")
        self.assertTrue(all(cell[2] is None for cell in folder[0] if cell[0] in ("▀", "▄", " ")
                            and cell[0] != "▀" or cell[2] is None))

    def test_accent_tints_folders_and_links_get_a_badge(self):
        plain = fs_icons.icon("folder", "large")
        tinted = fs_icons.icon("folder", "large", accent=(240, 130, 175))
        self.assertNotEqual(plain, tinted)
        self.assertNotEqual(fs_icons.icon("python", "large"), fs_icons.icon("python", "large", link=True))

    def test_gradients_and_colors_are_rgb(self):
        for kind in ("folder", "python", "image", "file"):
            self.assertEqual(len(fs_icons.color(kind)), 3)
            self.assertEqual(len(fs_icons.gradient(kind)), 3)


if __name__ == "__main__":
    unittest.main()
