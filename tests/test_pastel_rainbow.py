import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import fs_browser as fsb
import usage_panel as usage
from test_fs_browser import Base

RGB = re.compile(r'\x1b\[38;2;(\d+);(\d+);(\d+)m')
ANSI = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')


def colored_chars(rendered):
    """Return visible characters paired with the active true-color foreground."""
    color, result = None, []
    for token in re.findall(r'\x1b\[[0-9;?]*[A-Za-z]|.', rendered):
        match = RGB.fullmatch(token)
        if match:
            color = tuple(map(int, match.groups()))
        elif token == '\x1b[0m':
            color = None
        elif not token.startswith('\x1b'):
            result.append((token, color))
    return result


def assert_pastel(test, colors):
    test.assertGreater(len(set(colors)), 2)
    test.assertTrue(all(color and min(color) >= 191 for color in colors), colors)


class PastelRainbowTests(unittest.TestCase):
    def test_filename_gradient_uses_pastel_multicolor(self):
        colors = [cell[1] for cell in fsb.label_cells('rainbow', 'rainbow.txt')]
        assert_pastel(self, colors)

    def test_usage_picker_rainbow_label_and_swatches_are_pastel(self):
        menu = usage.ThemeMenu('brand')
        menu.index = menu.items.index('rainbow')
        for cols, rows in ((100, 14), (30, 5)):
            with self.subTest(layout='grid' if cols >= 60 else 'list'):
                rendered = '\n'.join(usage.theme_panel(menu, cols, rows, 0))
                visible = ANSI.sub('', rendered)
                self.assertIn('rainbow', visible)
                lines = rendered.splitlines()
                label_line = next(i for i, line in enumerate(lines)
                                  if 'rainbow' in ANSI.sub('', line))
                rainbow_row = lines[label_line]
                chars = colored_chars(rainbow_row)
                label_at = ANSI.sub('', rainbow_row).find('rainbow')
                label_colors = [color for _, color in chars[label_at:label_at + len('rainbow')]]
                assert_pastel(self, label_colors)
                swatch_line = rainbow_row if cols < 60 else lines[label_line + 1]
                swatches = colored_chars(swatch_line)
                if cols >= 60:
                    column = menu.index % menu.columns
                    swatches = swatches[column * 25:(column + 1) * 25]
                swatch_colors = [color for char, color in swatches if char == '▰']
                assert_pastel(self, swatch_colors)


class FilesystemPastelRainbowTests(Base):
    def test_theme_picker_rainbow_label_and_swatches_are_pastel(self):
        app = self.app()
        app.theme_menu = fsb.ThemeMenu('usage')
        app.theme_menu.index = app.theme_menu.items.index('rainbow')
        app.preview_theme, app.screen = 'rainbow', 'themes'
        rendered = app.theme_panel(100, 20, 0)
        rainbow_line = next(item for item in rendered
                            if isinstance(item, str) and 'rainbow' in ANSI.sub('', item))
        chars = colored_chars(rainbow_line)
        visible = ANSI.sub('', rainbow_line)
        label_at = visible.find('rainbow')
        assert_pastel(self, [color for _, color in chars[label_at:label_at + len('rainbow')]])
        assert_pastel(self, [color for char, color in chars if char == '▰'])


if __name__ == '__main__':
    unittest.main()
