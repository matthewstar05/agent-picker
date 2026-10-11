"""Real fake-provider processes, including owner crashes and child tools."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock

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

    @unittest.skipIf(os.name == "nt", "Windows ends leftover tools with the guardian's job")
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


# The template npm's cmd-shim writes for a package bin on Windows.
NPM_SHIM = r'''@ECHO off
GOTO start
:find_dp0
SET dp0=%~dp0
EXIT /b
:start
SETLOCAL
CALL :find_dp0

IF EXIST "%dp0%\node.exe" (
  SET "_prog=%dp0%\node.exe"
) ELSE (
  SET "_prog=node"
  SET PATHEXT=%PATHEXT:;.JS;=;%
)

endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  "%dp0%\node_modules\@openai\codex\bin\codex.js" %*
'''


class BatchLauncherTests(unittest.TestCase):
    """cmd.exe re-parses a .cmd file's arguments, so a prompt never goes through one."""

    PROMPT = 'fix "this" & del /q *\nthen run %PATH% | more'

    def test_an_npm_shim_runs_its_script_with_the_prompt_unchanged(self):
        from platform_runtime import direct_argv
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            shim = root / "codex.cmd"
            shim.write_text(NPM_SHIM)
            script = root / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
            script.parent.mkdir(parents=True)
            script.write_text("")
            (root / "node.exe").write_text("")
            argv = direct_argv([str(shim), "exec", "--", self.PROMPT], windows=True)
            self.assertEqual(argv, [str(root / "node.exe"), str(script), "exec", "--", self.PROMPT])

    def test_an_older_npm_shim_and_a_missing_script(self):
        from platform_runtime import direct_argv, shim_target
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            shim = root / "claude.cmd"
            shim.write_text('@IF EXIST "%~dp0\\node.exe" (\r\n  "%~dp0\\node.exe"  '
                            '"%~dp0\\node_modules\\@anthropic-ai\\claude-code\\cli.js" %*\r\n) ELSE (\r\n'
                            '  node  "%~dp0\\node_modules\\@anthropic-ai\\claude-code\\cli.js" %*\r\n)\r\n')
            self.assertIsNone(shim_target(shim))   # the script it names is not there
            script = root / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
            script.parent.mkdir(parents=True)
            script.write_text("")
            with unittest.mock.patch("platform_runtime.shutil.which", return_value="C:/node/node.exe"):
                self.assertEqual(direct_argv([str(shim), "-p", self.PROMPT], windows=True),
                                 ["C:/node/node.exe", str(script), "-p", self.PROMPT])

    def test_an_unknown_batch_launcher_refuses_cmd_syntax_but_takes_plain_words(self):
        from platform_runtime import UnsafeLauncher, direct_argv
        with tempfile.TemporaryDirectory() as folder:
            launcher = Path(folder) / "agent.BAT"
            launcher.write_text("@echo off\r\nC:\\tools\\agent.exe %*\r\n")
            for prompt in (self.PROMPT, "two\nlines", "100%", 'say "hi"', "a|b", "a^b"):
                with self.assertRaises(UnsafeLauncher, msg=prompt):
                    direct_argv([str(launcher), "-p", prompt], windows=True)
            plain = [str(launcher), "-p", "rename the helper (and its tests)!"]
            self.assertEqual(direct_argv(plain, windows=True), plain)
            # Other platforms and executables pass through untouched.
            self.assertEqual(direct_argv([str(launcher), self.PROMPT], windows=False), [str(launcher), self.PROMPT])
            self.assertEqual(direct_argv(["C:/x/codex.exe", self.PROMPT], windows=True), ["C:/x/codex.exe", self.PROMPT])

    def test_a_refused_launcher_fails_the_turn_before_any_process_starts(self):
        import platform_runtime
        with tempfile.TemporaryDirectory() as folder:
            launcher = Path(folder) / "agent.cmd"
            launcher.write_text("@echo off\r\nagent.exe %*\r\n")
            with unittest.mock.patch.object(star_engines, "direct_argv",
                                            lambda argv: platform_runtime.direct_argv(argv, windows=True)), \
                    unittest.mock.patch.object(star_engines.subprocess, "Popen") as popen:
                with self.assertRaises(OSError) as caught:
                    star_engines.Turn(None, [str(launcher), "-p", self.PROMPT])
            popen.assert_not_called()
            self.assertIn("batch launcher", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
