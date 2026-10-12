"""Real fake-provider processes, including owner crashes and child tools."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import star_engines
from star_mesh import alive
from star_sessions import ConversationBusy, Lease


class Echo:
    @staticmethod
    def parse(data, state):
        return [{"kind": "text", "data": data}]


class ProviderOwnershipTests(unittest.TestCase):
    def until(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.03)
        self.assertTrue(predicate(), "provider process did not reach the expected state")

    def test_owner_crash_stops_provider_and_tool_before_resume(self):
        self.exercise_stop(crash=True)

    def test_interrupt_stops_provider_and_tool(self):
        self.exercise_stop(crash=False)

    def exercise_stop(self, crash):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            tool = root / "tool.py"
            tool.write_text("import time\ntime.sleep(60)\n")
            provider = root / "provider.py"
            provider.write_text(
                "import json, os, pathlib, subprocess, sys, time\n"
                "child = subprocess.Popen([sys.executable, 'tool.py'])\n"
                "pathlib.Path('pids.json').write_text(json.dumps([os.getpid(), child.pid]))\n"
                "time.sleep(60)\n")
            owner = root / "owner.py"
            owner.write_text(
                "import sys; sys.path.insert(0, " + repr(str(ROOT / "src")) + ")\n"
                "import star_engines\nfrom star_sessions import Lease\n"
                "with Lease('locks') as lease:\n"
                "    lease.claim('shared')\n"
                "    run = star_engines.Turn(star_engines.ClaudeEngine, "
                "[sys.executable, 'provider.py'], lease=lease)\n"
                "    print('ready', flush=True)\n"
                "    sys.stdin.readline()\n"
                "    run.interrupt()\n"
                "    list(run)\n")
            proc = subprocess.Popen([sys.executable, str(owner)], cwd=root, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            pids = []
            try:
                self.assertEqual(proc.stdout.readline().strip(), "ready")
                self.until(lambda: (root / "pids.json").exists())
                pids = json.loads((root / "pids.json").read_text())
                self.assertTrue(all(alive(pid) for pid in pids))
                with Lease(root / "locks") as contender:
                    with self.assertRaises(ConversationBusy):
                        contender.claim("shared")
                    if crash:
                        proc.kill()
                    else:
                        proc.stdin.write("interrupt\n")
                        proc.stdin.flush()
                    proc.wait(timeout=8)
                    # Parent death alone cannot free a conversation while its
                    # provider guardian is still stopping the old turn.
                    def resumed():
                        try:
                            contender.claim("shared")
                            return True
                        except ConversationBusy:
                            return False
                    self.until(resumed)
                    self.until(lambda: not any(alive(pid) for pid in pids))
                if not crash:
                    self.assertEqual(proc.returncode, 0, proc.stderr.read())
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=8)
                proc.stdin.close()
                proc.stdout.close()
                proc.stderr.close()

    def test_guardian_without_start_confirmation_never_launches_provider(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            proc = subprocess.Popen([sys.executable, str(ROOT / "src/star_process.py")],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, cwd=root)
            config = {"argv": [sys.executable, "-c", "open('launched', 'w').close()"],
                      "locks": str(root / "locks/turns"), "conversation": "shared"}
            try:
                proc.stdin.write(json.dumps(config) + "\n")
                proc.stdin.flush()
                self.assertIn('"ready"', proc.stdout.readline())
                with Lease(root / "locks") as lease:
                    with self.assertRaisesRegex(ConversationBusy, "provider stopping"):
                        lease.claim("shared")
                    proc.stdin.close()
                    proc.wait(timeout=5)
                    lease.claim("shared")
                self.assertFalse((root / "launched").exists())
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=5)
                proc.stdin.close()
                proc.stdout.close()
                proc.stderr.close()

    def test_native_launch_error_is_drained_before_reporting(self):
        run = star_engines.Turn(star_engines.ClaudeEngine, [str(ROOT / "not-installed-provider")])
        errors = [event["text"] for event in run if event["kind"] == "error"]
        self.assertEqual(len(errors), 1)
        self.assertIn("not-installed-provider", errors[0])

    def test_guardian_exits_cleanly_while_owner_keeps_control_pipe_open(self):
        # Closing stdin with communicate() would hide the shutdown bug: the
        # old daemon reader only held its lock while the owner's pipe stayed open.
        for code in (0, 7):
            for attempt in range(5):
                with self.subTest(code=code, attempt=attempt):
                    proc = subprocess.Popen([sys.executable, str(ROOT / "src/star_process.py")],
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, text=True)
                    try:
                        argv = [sys.executable, "-c", f"print('finished', flush=True); raise SystemExit({code})"]
                        proc.stdin.write(json.dumps({"argv": argv}) + "\n")
                        proc.stdin.flush()
                        self.assertIn('"ready"', proc.stdout.readline())
                        proc.stdin.write("go\n")
                        proc.stdin.flush()
                        self.assertEqual(proc.wait(timeout=5), code)
                        self.assertEqual(proc.stdout.read(), "finished\n")
                        self.assertEqual(proc.stderr.read(), "")
                    finally:
                        if proc.poll() is None:
                            proc.kill()
                        proc.wait(timeout=5)
                        proc.stdin.close()
                        proc.stdout.close()
                        proc.stderr.close()

    def test_owner_closing_output_pipes_does_not_crash_the_guardian(self):
        script = '''import os, signal, time
def finish(*_):
    for fd in (1, 2):
        try:
            os.write(fd, b'provider closing\\n')
        except BrokenPipeError:
            pass
    raise SystemExit(0)
signal.signal(signal.SIGINT, finish)
print('provider ready', flush=True)
while True:
    time.sleep(.1)
'''
        for close_stderr in (False, True):
            with self.subTest(close_stderr=close_stderr):
                proc = subprocess.Popen([sys.executable, str(ROOT / "src/star_process.py")],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True)
                try:
                    proc.stdin.write(json.dumps({"argv": [sys.executable, "-c", script]}) + "\n")
                    proc.stdin.flush()
                    self.assertIn('"ready"', proc.stdout.readline())
                    proc.stdin.write("go\n")
                    proc.stdin.flush()
                    self.assertEqual(proc.stdout.readline().strip(), "provider ready")
                    proc.stdout.close()
                    if close_stderr:
                        proc.stderr.close()
                    proc.stdin.close()
                    self.assertEqual(proc.wait(timeout=8), 0)
                    if not close_stderr:
                        self.assertEqual(proc.stderr.read(), "provider closing\n")
                finally:
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait(timeout=5)
                    proc.stdin.close()
                    proc.stdout.close()
                    proc.stderr.close()

    def test_stream_parser_failure_stops_the_owned_provider(self):
        class BrokenParser:
            @staticmethod
            def parse(data, state):
                raise ValueError("bad fixture event")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "provider.pid"
            script = ("import os, pathlib, time; pathlib.Path(" + repr(str(path))
                      + ").write_text(str(os.getpid())); print('{}', flush=True); time.sleep(60)")
            run = star_engines.Turn(BrokenParser, [sys.executable, "-c", script])
            with self.assertRaisesRegex(ValueError, "bad fixture event"):
                list(run)
            self.assertFalse(alive(int(path.read_text())))
            self.assertIsNotNone(run.proc.returncode)

    def test_a_tool_left_holding_the_output_pipe_does_not_hold_the_turn(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "tool.pid"
            tool = ("import os, pathlib, time; pathlib.Path(" + repr(str(path))
                    + ").write_text(str(os.getpid())); time.sleep(60)")
            # The provider's last output is larger than one pipe buffer and
            # must arrive whole although its tool keeps both pipes open.
            script = ("import json, subprocess, sys; subprocess.Popen([sys.executable, '-c', " + repr(tool) + "]); "
                      "print(json.dumps({'text': 'x' * 300000}), flush=True); sys.stderr.write('warned\\n')")
            started = time.monotonic()
            run = star_engines.Turn(Echo, [sys.executable, "-c", script])
            events = list(run)
            self.assertLess(time.monotonic() - started, 8)
            self.assertEqual([len(e["data"]["text"]) for e in events], [300000])
            self.assertIn("warned\n", run.stderr)
            self.until(path.exists)
            pid = int(path.read_text())
            try:
                # Finishing the turn does not end a tool the provider started.
                self.assertTrue(alive(pid))
            finally:
                os.kill(pid, 9)


if __name__ == "__main__":
    unittest.main()
