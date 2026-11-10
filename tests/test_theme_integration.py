import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import agent_picker as picker
import preferences
import themes
import usage_panel as panel


class ThemeIntegrationTests(unittest.TestCase):
    def test_label_metadata_accepts_rainbow_and_repository_colors(self):
        theme, error = themes._theme({'name': 'custom', 'stops': None,
                                      'file_color': 'rainbow', 'repo_color': '#abcdef'})
        self.assertIsNone(error)
        self.assertEqual(theme['file_color'], 'rainbow')
        self.assertEqual(theme['repo_color'], (171, 205, 239))
        for bad in ('red', [1, 2], [True, 2, 3]):
            theme, error = themes._theme({'name': 'bad', 'stops': None, 'repo_color': bad})
            self.assertIsNone(theme)
            self.assertIn('repo_color', error)

    def test_readable_label_helper_keeps_already_bright_colors(self):
        for color in ((255, 255, 255), (200, 220, 240), (232, 191, 104)):
            self.assertEqual(themes.label_color(color), color)

    def test_theme_selector_owns_keys_before_global_screen_shortcuts(self):
        keys = iter(('t', 'c', '\\', 'right', '\r', 'q'))

        class Terminal:
            def __enter__(self):
                return self

            def write(self, text):
                pass

            def keyboard(self, enabled):
                pass

            def wait(self, seconds):
                return True

            def read_key(self):
                return next(keys)

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(picker, 'Terminal', Terminal), \
                    patch.object(picker, 'STATE', root / 'state'), \
                    patch.object(picker, 'installed', side_effect=lambda a: a['name'] == 'Claude'), \
                    patch.object(picker, 'load_usage'), \
                    patch.object(picker.model_catalog, 'request_all'), \
                    patch.object(panel, 'start'), \
                    patch.object(preferences, 'PATH', root / 'preferences.json'), \
                    patch.object(preferences, '_values', {}), \
                    patch('sys.stdout', io.StringIO()):
                self.assertIsNone(picker.choose(screen='usage', only=['Claude']))
                self.assertEqual(preferences.get(panel.THEME.key), 'rainbow')

    def test_interrupt_still_quits_from_theme_selector(self):
        view = panel.View([], pinned=True)
        panel.handle(view, 't', [])
        self.assertEqual(panel.handle(view, '\x03', []), 'quit')
