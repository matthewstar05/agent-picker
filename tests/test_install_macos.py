"""Exercise selected launchers in disposable profiles, including shell activation."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import install_common


def installer():
    spec = importlib.util.spec_from_file_location('macos_install_test', ROOT / 'scripts/install-macos.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipIf(os.name == 'nt', 'POSIX shell launchers')
class MacosInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='picker install ')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.bin = self.home / 'my commands'
        self.module = installer()
        self.env = {'HOME': str(self.home), 'USERPROFILE': str(self.home),
                    'AGENT_PICKER_STATE_HOME': str(self.home / 'state'),
                    'AGENT_PICKER_CONFIG': str(self.home / 'config.json')}
        env = patch.dict(os.environ, self.env)
        env.start()
        self.addCleanup(env.stop)

    def install(self, *options):
        with contextlib.redirect_stdout(io.StringIO()):
            return self.module.main(['--prefix', str(self.bin), *options])

    def test_all_launchers_forward_arguments_and_exit_status_from_spaced_checkout(self):
        checkout = self.home / 'source checkout'
        source = checkout / 'src'
        source.mkdir(parents=True)
        stub = 'import json,sys\nprint(json.dumps(sys.argv[1:]))\nraise SystemExit(7)\n'
        commands = install_common.selected_commands(install_common.COMPONENTS)
        for script, _ in commands.values():
            (source / script).write_text(stub, encoding='utf-8')
        args = ['a prompt with spaces', '$(printf BAD)', "a'quote", '--user-flag']
        with patch.object(self.module, 'ROOT', checkout):
            self.install('--all', '--no-path')
            for name, (_, extra) in commands.items():
                with self.subTest(command=name):
                    result = subprocess.run([str(self.bin / name), *args], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 7, result.stderr)
                    self.assertEqual(json.loads(result.stdout), extra + args)
            self.install('--components', 'none', '--no-path')
            self.assertEqual({p.name for p in self.bin.iterdir()}, {'agent', 'agent-picker'})

    def test_narrower_selection_removes_only_owned_launchers_with_backup(self):
        self.install('--all')
        custom = self.bin / 'grokbotcli'
        custom.write_text('#!/bin/sh\necho custom\n')
        foreign = self.bin / 'starcli'
        foreign.write_text('#!/bin/sh\n' + install_common.MANAGED_MARKER + '\nexec /other/checkout/src/starcli.py\n')
        self.install('--components', 'usage,fs')
        self.assertFalse((self.bin / 'star').exists())
        self.assertTrue((self.bin / 'usage').exists())
        self.assertTrue((self.bin / 'herdr-fs').exists())
        self.assertIn('echo custom', custom.read_text())
        self.assertIn('/other/checkout', foreign.read_text())
        self.assertTrue(list((self.home / 'state/backups').glob('*/star')))
        text = (self.home / '.zshrc').read_text()
        self.assertNotIn('alias star=', text)
        self.assertIn('alias fs=', text)
        self.assertEqual(text.count(self.module.START), 1)

    @unittest.skipUnless(shutil.which('bash'), 'bash required')
    def test_fresh_shell_resolves_shortcuts_and_preserves_profile(self):
        rc = self.home / '.bashrc'
        rc.write_text('export USER_SETTING=keep\n# Kiro CLI post block. Keep at the bottom of this file.\n# tail\n')
        self.install('--components', 'fs')
        original = rc.read_text()
        self.install('--components', 'fs')
        self.assertEqual(rc.read_text(), original)
        self.assertTrue(original.endswith('# tail\n'))
        result = subprocess.run([shutil.which('bash'), '--noprofile', '--rcfile', str(rc), '-ic',
                                 'test "$USER_SETTING" = keep && agent --picker-doctor && fs --help'],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('agent-picker', result.stdout)
        self.assertIn('fs', result.stdout)

    def test_existing_bash_login_profile_keeps_precedence(self):
        login = self.home / '.bash_login'
        login.write_text('export LOGIN_SETTING=keep\n')
        self.install('--components', 'none')
        self.assertFalse((self.home / '.bash_profile').exists())
        self.assertIn('export LOGIN_SETTING=keep', login.read_text())
        self.assertIn(self.module.START, login.read_text())

    def test_no_path_leaves_profiles_untouched(self):
        rc = self.home / '.zshrc'
        rc.write_text('export KEEP=yes\n')
        self.install('--components', 'none', '--no-path')
        self.assertEqual(rc.read_text(), 'export KEEP=yes\n')
        self.assertFalse((self.home / '.bashrc').exists())
        self.assertEqual({p.name for p in self.bin.iterdir()}, {'agent', 'agent-picker'})


if __name__ == '__main__':
    unittest.main()
