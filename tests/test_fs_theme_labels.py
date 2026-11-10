import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import fs_browser as fsb
import themes
from test_fs_browser import Base


def contrast(color):
    return (themes._luminance(color) + 0.05) / (themes._luminance(themes.LABEL_BACKGROUND) + 0.05)


class FilenameThemeTests(unittest.TestCase):
    def test_builtin_colors_are_readable_and_repositories_are_distinct(self):
        for theme in themes._files['builtin'][2]:
            name = theme['name']
            with self.subTest(theme=name):
                for phase in (0, 0.25, 0.5, 0.75):
                    file_color = fsb.filename_color(name, phase=phase)
                    repo_color = fsb.filename_color(name, kind='folder-github', phase=phase)
                    self.assertGreaterEqual(contrast(file_color), 7)
                    self.assertGreaterEqual(contrast(repo_color), 7)
                    self.assertNotEqual(file_color, repo_color)

    def test_rainbow_label_and_repository_vary_by_character(self):
        file_cells = fsb.label_cells('rainbow', 'example.txt')
        repo_cells = fsb.label_cells('rainbow', 'example.txt', kind='folder-github')
        self.assertGreater(len({c[1] for c in file_cells}), 2)
        self.assertNotEqual(file_cells[0][1], repo_cells[0][1])
        self.assertTrue(all(contrast(c[1]) >= 7 for c in file_cells + repo_cells))

    def test_wrapped_labels_fit_and_keep_theme_colors(self):
        entry = SimpleNamespace(name='long-file-name.txt', kind='folder', hidden=False)
        for width in range(4, 20):
            lines = fsb.filename_cells(entry, width, fsb.WHITE, lines=2, theme='rainbow')
            self.assertTrue(all(len(line) <= width for line in lines))
            self.assertTrue(all(contrast(c[1]) >= 7 for line in lines for c in line))

    def test_fallback_and_selection_states(self):
        custom = {'name': 'custom', 'stops': [(100, 120, 180), (120, 150, 200)]}
        with patch.object(themes, 'get', return_value=custom), patch.object(themes, 'stops', return_value=custom['stops']):
            normal = fsb.filename_color('custom')
            repo = fsb.filename_color('custom', kind='folder-github')
            hidden = fsb.filename_color('custom', hidden=True)
            selected = fsb.filename_color('custom', selected=True)
        self.assertGreaterEqual(contrast(normal), 7)
        self.assertGreaterEqual(contrast(repo), 7)
        self.assertNotEqual(normal, repo)
        self.assertLess(sum(hidden), sum(normal))
        self.assertGreater(sum(selected), sum(normal))


class RenderedFilenameThemeTests(Base):
    def setUp(self):
        super().setUp()
        (self.tree / 'rainbow.txt').write_text('hello')

    def captured_names(self, app, cols, rows):
        captured = []
        original = fsb.paint

        def record(cells):
            captured.append(list(cells))
            return original(cells)

        with patch.object(fsb, 'paint', side_effect=record):
            app.frame(cols, rows, 1.0)
        matches = []
        for cells in captured:
            text = ''.join(c[0] for c in cells)
            start = text.find('rainbow.txt')
            if start >= 0:
                matches.append(cells[start:start + len('rainbow.txt')])
        return matches

    def test_rainbow_is_rendered_in_icons_details_and_tiny_views(self):
        for view, cols, rows in (('icons', 100, 20), ('details', 100, 12), ('icons', 18, 4)):
            with self.subTest(view=view, cols=cols):
                app = self.app(view=view)
                app.preview_theme = 'rainbow'
                app.index = self.names(app).index('rainbow.txt')
                matches = self.captured_names(app, cols, rows)
                self.assertTrue(matches)
                self.assertGreater(len({c[1] for c in matches[0]}), 2)

    def test_rainbow_is_rendered_in_preview_header_and_info(self):
        app = self.app()
        app.preview_theme = 'rainbow'
        entry = next(e for e in app.entries if e.name == 'rainbow.txt')
        app.open_preview(entry)
        app.preview.mode, app.preview.loading = 'info', False
        matches = self.captured_names(app, 100, 20)
        self.assertGreaterEqual(len(matches), 2)
        for cells in matches:
            self.assertGreater(len({c[1] for c in cells}), 2)


if __name__ == '__main__':
    unittest.main()
