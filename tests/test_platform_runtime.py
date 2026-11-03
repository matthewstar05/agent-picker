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
