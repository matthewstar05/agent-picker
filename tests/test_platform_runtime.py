import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from platform_runtime import pipe_ready, process_alive, stop_process_tree
import platform_runtime
import file_lock


class PortableRuntimeTests(unittest.TestCase):
    def test_windows_events_distinguish_accented_text_and_extended_keys(self):
        from platform_runtime import windows_key_bytes
        self.assertEqual(windows_key_bytes("à", 65, 0), "à".encode())
        self.assertEqual(windows_key_bytes("😀", 0, 0), "😀".encode())
        self.assertEqual(windows_key_bytes("", 0x26, 0), b"\x1b[A")
        self.assertEqual(windows_key_bytes("", 0x25, 0x10), b"\x1b[1;2D")
        self.assertEqual(windows_key_bytes("€", 69, 0x2 | 0x8), "€".encode())

    @unittest.skipUnless(sys.platform == "win32", "requires Windows process enumeration")
    def test_windows_process_arguments_preserve_unicode(self):
        from platform_runtime import windows_processes
        marker = "provider workspace α 中文"
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", marker])
        try:
            rows = {pid: argv for pid, _, argv in windows_processes()}
            self.assertIn(child.pid, rows)
            self.assertIn(marker, rows[child.pid])
        finally:
            child.terminate()
            child.wait(timeout=10)

    def test_pipe_reports_data_and_eof(self):
        child = subprocess.Popen([sys.executable, "-c", "print('hello', flush=True)"], stdout=subprocess.PIPE)
        try:
            self.assertTrue(pipe_ready(child.stdout.fileno(), 10))
            self.assertEqual(child.stdout.readline().strip(), b"hello")
            child.wait(timeout=10)
            self.assertTrue(pipe_ready(child.stdout.fileno(), 1))
            self.assertEqual(child.stdout.read(), b"")
        finally:
            child.stdout.close()
            if child.poll() is None:
                child.kill()
                child.wait()

    def test_liveness_probe_does_not_terminate_a_child(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        try:
            self.assertTrue(process_alive(child.pid))
            self.assertIsNone(child.poll())
            stop_process_tree(child)
            self.assertFalse(process_alive(child.pid))
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()

    def test_process_lock_excludes_a_second_interpreter(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "one-writer.lock"
            script = """import sys
import file_lock
with open(sys.argv[1], 'a+b') as handle:
    try:
        file_lock.flock(handle, file_lock.LOCK_EX | file_lock.LOCK_NB)
    except BlockingIOError:
        print('locked')
    else:
        print('acquired')
"""
            env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
            with path.open("a+b") as handle:
                file_lock.flock(handle, file_lock.LOCK_EX)
                result = subprocess.run([sys.executable, "-c", script, str(path)], capture_output=True, text=True, env=env, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "locked")
                file_lock.flock(handle, file_lock.LOCK_UN)
            result = subprocess.run([sys.executable, "-c", script, str(path)], capture_output=True, text=True, env=env, timeout=10)
            self.assertEqual(result.stdout.strip(), "acquired", result.stderr)

    @unittest.skipUnless(sys.platform == "win32", "requires the native Windows console")
    def test_native_windows_console_and_first_run_connections(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            marker = path / "console-result.json"
            env = dict(os.environ, AGENT_PICKER_STATE_HOME=str(path / "state"),
                       AGENT_PICKER_CONFIG=str(path / "config.json"))
            result = subprocess.run([sys.executable, str(ROOT / "tests/native_windows_console.py"), str(marker)], env=env,
                                    creationflags=subprocess.CREATE_NEW_CONSOLE, timeout=30)
            self.assertEqual(result.returncode, 0, marker.read_text(encoding="utf-8") if marker.exists() else "No console result")
            self.assertEqual(json.loads(marker.read_text(encoding="utf-8")), {"console_input": "passed", "connections": "passed"})


class BatchShimTests(unittest.TestCase):
    """Free text must not pass through cmd.exe: it cuts prompts at line breaks."""

    NPM = ('@ECHO off\r\nGOTO start\r\n:find_dp0\r\nSET dp0=%~dp0\r\nEXIT /b\r\n:start\r\nSETLOCAL\r\n'
           'CALL :find_dp0\r\n\r\nIF EXIST "%dp0%\\node.exe" (\r\n  SET "_prog=%dp0%\\node.exe"\r\n) ELSE (\r\n'
           '  SET "_prog=node"\r\n  SET PATHEXT=%PATHEXT:;.JS;=;%\r\n)\r\n\r\n'
           'endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  '
           '"%dp0%\\node_modules\\@openai\\codex\\bin\\codex.js" %*\r\n')

    def direct(self, argv):
        from unittest.mock import patch
        with patch.object(platform_runtime.sys, "platform", "win32"):
            return platform_runtime.direct_argv(argv)

    def test_npm_shim_runs_node_with_the_package_script(self):
        with tempfile.TemporaryDirectory() as folder:
            shim = Path(folder) / "codex.cmd"
            shim.write_text(self.NPM, encoding="utf-8")
            (Path(folder) / "node.exe").write_bytes(b"MZ")
            prompt = "line one\nline two & 100% \"quoted\""
            argv = self.direct([str(shim), "exec", "--", prompt])
        self.assertEqual(argv[0], str(Path(folder) / "node.exe"))
        self.assertTrue(argv[1].endswith("node_modules\\@openai\\codex\\bin\\codex.js"))
        self.assertTrue(argv[1].startswith(folder))
        self.assertEqual(argv[2:], ["exec", "--", prompt])

    def test_fixed_command_shim_and_ampersand_in_its_path(self):
        with tempfile.TemporaryDirectory() as folder:
            shim = Path(folder) / "fake.cmd"
            shim.write_text('@echo off\r\n"C:\\Tools & Co\\python.exe" "C:\\x\\fake.py" %*\r\nexit /b %errorlevel%\r\n')
            self.assertEqual(self.direct([str(shim), "-p", "a\nb"]),
                             ["C:\\Tools & Co\\python.exe", "C:\\x\\fake.py", "-p", "a\nb"])

    def test_unknown_batch_file_refuses_text_cmd_would_change(self):
        with tempfile.TemporaryDirectory() as folder:
            shim = Path(folder) / "odd.cmd"
            shim.write_text("@echo off\r\nset X=1\r\ncall %X% %*\r\n")
            self.assertEqual(self.direct([str(shim), "--flag", "plain words"]), [str(shim), "--flag", "plain words"])
            for text in ("two\nlines", "50%", 'say "hi"'):
                with self.subTest(text=text), self.assertRaisesRegex(ValueError, "batch file"):
                    self.direct([str(shim), "-p", text])

    def test_other_platforms_and_executables_are_unchanged(self):
        from unittest.mock import patch
        with patch.object(platform_runtime.sys, "platform", "darwin"):
            self.assertEqual(platform_runtime.direct_argv(["codex.cmd", "a\nb"]), ["codex.cmd", "a\nb"])
        self.assertEqual(self.direct(["C:\\bin\\codex.exe", "a\nb"]), ["C:\\bin\\codex.exe", "a\nb"])
