import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import agent_picker as ap
import preferences
import themes
import usage_panel as panel

ANSI = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')


class UsageThemePickerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for p in (patch.object(preferences, 'PATH', Path(self.temp.name) / 'prefs.json'),
                  patch.object(preferences, '_values', {})):
            p.start()
            self.addCleanup(p.stop)
        self.view = panel.View(ap.AGENTS[:2], pinned=True)

    def test_preview_cancel_and_apply_intercept_navigation(self):
        original = preferences.get(panel.THEME.key)
        panel.handle(self.view, 't', [])
        panel.render(self.view, 100, 8, 0)
        panel.handle(self.view, 'right', [])
        selected = self.view.theme_menu.value
        self.assertNotEqual(selected, original)
        self.assertEqual(preferences.get(panel.THEME.key), original)
        self.assertEqual(panel.THEMES[self.view.theme]['name'], selected)
        self.assertIsNone(panel.handle(self.view, 's', []))
        panel.handle(self.view, 'esc', [])
        self.assertIsNone(self.view.theme_menu)
        self.assertEqual(preferences.get(panel.THEME.key), original)
        panel.handle(self.view, 't', [])
        panel.handle(self.view, 'down', [])
        selected = self.view.theme_menu.value
        panel.handle(self.view, '\r', [])
        self.assertEqual(preferences.get(panel.THEME.key), selected)

    def test_grid_list_resize_and_all_items_reachable(self):
        panel.handle(self.view, 't', [])
        menu = self.view.theme_menu
        for cols, rows in ((100, 14), (30, 5), (8, 1), (1, 1), (0, 0)):
            lines = panel.render(self.view, cols, rows, 0)
            self.assertLessEqual(len(lines), rows)
            self.assertTrue(all(len(ANSI.sub('', line)) <= cols for line in lines))
        grid = panel.render(self.view, 100, 14, 0)
        plain = '\n'.join(ANSI.sub('', line) for line in grid)
        self.assertIn('╭', plain)
        self.assertIn('▰', plain)
        self.assertIn('❯ brand ✓', plain)
        self.assertGreater(menu.columns, 1)
        panel.handle(self.view, 'down', [])
        self.assertEqual(menu.index, menu.columns)
        panel.render(self.view, 30, 5, 0)
        self.assertEqual(menu.columns, 1)
        seen = set()
        for _ in menu.items:
            seen.add(menu.value)
            text = ''.join(ANSI.sub('', line) for line in panel.render(self.view, 30, 5, 0))
            self.assertIn(menu.value, text)
            panel.handle(self.view, 'down', [])
        self.assertEqual(seen, set(menu.items))

    def test_title_colors_in_full_compact_and_working_cards(self):
        def luminance(rgb):
            channels = [v / 255 for v in rgb]
            return sum(w * (v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4)
                       for w, v in zip((0.2126, 0.7152, 0.0722), channels))

        def cells(frame):
            text, colors, color = '', [], None
            source = '\n'.join(''.join(row) if isinstance(row, tuple) else row for row in frame)
            for token in re.findall(r'\x1b\[[0-9;?]*[A-Za-z]|.', source):
                if token.startswith('\x1b[38;2;'):
                    color = tuple(map(int, token[7:-1].split(';')))
                elif token == '\x1b[0m':
                    color = None
                elif not token.startswith('\x1b'):
                    text += token
                    colors.append(color)
            return text, colors

        agent = self.view.agents[0]
        background = luminance((40, 40, 48))
        for theme in panel.THEMES:
            preferences.put(panel.THEME.key, theme['name'])
            self.view.toast_until = float('inf')
            for cols, rows, working in ((100, 20, False), (20, 2, False), (100, 20, True)):
                with patch.dict(panel.WORKING, {agent['name']: 1} if working else {}, clear=True):
                    text, colors = cells(panel.render(self.view, cols, rows, 1.5))
                targets = [agent['name']]
                if cols == 100:
                    targets += ['usage', theme['name']]
                for target in targets:
                    offset = text.find(target)
                    self.assertGreaterEqual(offset, 0, (theme['name'], cols, target))
                    for color in colors[offset:offset + len(target)]:
                        self.assertIsNotNone(color, (theme['name'], cols, target))
                        ratio = (luminance(color) + 0.05) / (background + 0.05)
                        self.assertGreaterEqual(ratio, 7, (theme['name'], cols, target, color))

    def test_provider_names_use_readable_color(self):
        agent = {'name': 'Dark', 'color': (1, 2, 3)}
        line = panel.Line()
        panel.put_name(line, agent, 0)
        self.assertEqual(line.cells[0][1], themes.label_color(agent['color']))


if __name__ == '__main__':
    unittest.main()
