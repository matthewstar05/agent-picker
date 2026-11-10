import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import install_common as common


def load_dispatcher():
    spec = importlib.util.spec_from_file_location('installer_dispatch_test', SCRIPTS / 'install.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class InstallOptionsTests(unittest.TestCase):
    def parse(self, args):
        with contextlib.redirect_stdout(io.StringIO()):
            return common.parse_options(args)

    def test_explicit_selection_and_exclusion_are_ordered(self):
        options = self.parse(['--components', 'herdr,usage,fs', '--without', 'fs', '--prefix', '/tmp/example', '--no-path'])
        self.assertEqual(options.components, ['usage', 'herdr'])
        self.assertEqual(options.prefix, Path('/tmp/example'))
        self.assertTrue(options.no_path)
        self.assertEqual(self.parse(['--components', 'none']).components, [])
        self.assertEqual(self.parse(['--without', 'fs']).components,
                         [key for key in common.COMPONENTS if key != 'fs'])

    def test_invalid_names_and_empty_entries_fail(self):
        for value in ('', 'fs,', ',fs', 'fs,unknown', 'none,fs'):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    self.parse(['--components', value])
                self.assertEqual(caught.exception.code, 2)

    def test_noninteractive_defaults_and_yes_never_prompt(self):
        with mock.patch.object(common.sys.stdin, 'isatty', return_value=False), mock.patch('builtins.input', side_effect=AssertionError):
            self.assertEqual(self.parse([]).components, list(common.COMPONENTS))
        with mock.patch.object(common.sys.stdin, 'isatty', return_value=True), mock.patch('builtins.input', side_effect=AssertionError):
            self.assertEqual(self.parse(['--yes']).components, list(common.COMPONENTS))
            self.assertEqual(self.parse(['--all']).components, list(common.COMPONENTS))

    def test_interactive_toggle_and_cancel(self):
        with mock.patch.object(common.sys.stdin, 'isatty', return_value=True), mock.patch('builtins.input', side_effect=['none', '1 2', '2', '']):
            self.assertEqual(self.parse([]).components, ['usage'])
        with mock.patch.object(common.sys.stdin, 'isatty', return_value=True), mock.patch('builtins.input', return_value='q'):
            with self.assertRaises(SystemExit) as caught:
                self.parse([])
            self.assertEqual(caught.exception.code, 0)

    def test_catalog_and_list_do_not_launch_providers(self):
        self.assertEqual(set(common.selected_commands([])), {'agent', 'agent-picker'})
        selected = common.selected_commands(['starcli', 'grokbotcli', 'usage'])
        self.assertEqual(selected['usage'], ('agent_picker.py', ['--picker-usage']))
        self.assertTrue({'star', 'starcli', 'grok', 'grokbot', 'grokbotcli'} <= set(selected))
        with contextlib.redirect_stdout(io.StringIO()) as output, mock.patch('subprocess.run', side_effect=AssertionError):
            with self.assertRaises(SystemExit) as caught:
                common.parse_options(['--list-components'])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn('grokbotcli:', output.getvalue())

    def test_dispatch_selects_platform_and_forwards_arguments(self):
        dispatcher = load_dispatcher()
        fake = mock.Mock()
        fake.main.return_value = 7
        def change_path(module):
            dispatcher.sys.path.insert(0, 'temporary-src')
        fake_spec = mock.Mock()
        fake_spec.loader.exec_module.side_effect = change_path
        for platform, filename in [('darwin', 'install-macos.py'), ('win32', 'install-windows.py')]:
            with self.subTest(platform=platform), mock.patch.object(dispatcher.sys, 'platform', platform), \
                    mock.patch.object(dispatcher.importlib.util, 'module_from_spec', return_value=fake), \
                    mock.patch.object(dispatcher.importlib.util, 'spec_from_file_location') as make_spec:
                make_spec.return_value = fake_spec
                original_path = dispatcher.sys.path[:]
                self.assertEqual(dispatcher.main(['--yes']), 7)
                self.assertEqual(dispatcher.sys.path, original_path)
                self.assertEqual(make_spec.call_args.args[1].name, filename)
                fake.main.assert_called_with(['--yes'])
        with mock.patch.object(dispatcher.sys, 'platform', 'linux'):
            with self.assertRaises(SystemExit):
                dispatcher.main([])


if __name__ == '__main__':
    unittest.main()
