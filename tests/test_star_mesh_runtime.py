"""Exercise real StarCLI processes and their editor wakeups with a fake provider."""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class MeshRuntimeTests(unittest.TestCase):
    def test_crashed_goal_reopens_with_its_native_session_and_resumes_automatically(self):
        sys.path.insert(0, str(ROOT / "src"))
        from star_sessions import Lease, ConversationBusy
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            fake = root / "fake-claude"
            fake.write_text(textwrap.dedent(f'''\
                #!{sys.executable}
                import json, os, pathlib, sys, time
                marker = pathlib.Path("partial.txt")
                first = not marker.exists()
                print(json.dumps({{"type": "system", "subtype": "init", "session_id": "crash-session"}}), flush=True)
                if first:
                    marker.write_text("preserved effect")
                    pathlib.Path("provider.pid").write_text(str(os.getpid()))
                    time.sleep(60)
                else:
                    assert marker.read_text() == "preserved effect"
                    assert "--resume" in sys.argv and "crash-session" in sys.argv
                    assert "previous Star process stopped" in sys.argv[2]
                    pathlib.Path("recovered.json").write_text(json.dumps(sys.argv))
                    print(json.dumps({{"type": "stream_event", "event": {{"delta": {{
                        "type": "text_delta", "text": "GOAL COMPLETE"}}}}}}), flush=True)
                    print(json.dumps({{"type": "result", "session_id": "crash-session", "usage": {{}}}}), flush=True)
                '''))
            fake.chmod(0o755)
            config = root / "config.json"
            profiles = json.loads((ROOT / "agents.json").read_text())
            config.write_text(json.dumps({"agents": {
                a["name"]: {"command": str(fake if a["name"] == "Claude" else root / "not-installed")}
                for a in profiles if a["name"] != "Star"}}))
            env = {k: v for k, v in os.environ.items() if not k.startswith(("HERDR_", "STAR_"))}
            env.update(AGENT_PICKER_STATE_HOME=str(root / "state"), AGENT_PICKER_CONFIG=str(config), NO_COLOR="1")
            driver = (f"import sys; sys.path.insert(0, {str(ROOT / 'src')!r}); import starcli; "
                      "starcli.refresh_meters = lambda *a, **k: None; "
                      "starcli.report_herdr = lambda *a, **k: None; sys.exit(starcli.main())")
            proc = subprocess.Popen([sys.executable, "-c", driver, "--use", "Claude", "--goal", "finish the local task"],
                                    cwd=root, env=env, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE, text=True, start_new_session=True)
            try:
                saved = None
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    paths = list((root / "state/star/conversations").glob("*.json"))
                    if paths:
                        saved = json.loads(paths[0].read_text())
                        if saved.get("sessions", {}).get("Claude") and (root / "provider.pid").exists():
                            break
                    time.sleep(.03)
                self.assertIsNotNone(saved)
                self.assertEqual(saved["goal_state"], "running")
                self.assertEqual(saved["sessions"]["Claude"], "crash-session")
                self.assertIn("finish the local task", saved["inflight"]["prompt"])
                proc.kill()
                proc.wait(timeout=5)
                with Lease(root / "state/star/conversations/locks") as lease:
                    deadline = time.monotonic() + 8
                    while True:
                        try:
                            lease.claim(saved["id"])
                            break
                        except ConversationBusy:
                            self.assertLess(time.monotonic(), deadline)
                            time.sleep(.03)
                resumed = subprocess.run([sys.executable, "-c", driver, "--resume-id", saved["id"]],
                                         cwd=root, env=env, input="/quit\n", capture_output=True, text=True, timeout=10)
                self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
                self.assertTrue((root / "recovered.json").exists(), resumed.stdout)
                final = json.loads(paths[0].read_text())
                self.assertEqual(final["goal"], "")
                self.assertIsNone(final["inflight"])
                self.assertEqual(final["turns"][0]["status"], "interrupted")
                self.assertEqual((root / "partial.txt").read_text(), "preserved effect")
                # Reopening deliberately paused or blocked goals must not
                # authorize another provider turn.
                receipt = (root / "recovered.json").read_text()
                for state in ("paused", "blocked"):
                    final.update(goal="wait for the user", goal_state=state)
                    paths[0].write_text(json.dumps(final))
                    reopened = subprocess.run([sys.executable, "-c", driver, "--resume-id", saved["id"]],
                                              cwd=root, env=env, input="/quit\n", capture_output=True,
                                              text=True, timeout=10)
                    self.assertEqual(reopened.returncode, 0, reopened.stdout + reopened.stderr)
                    self.assertEqual((root / "recovered.json").read_text(), receipt)
                    self.assertEqual(json.loads(paths[0].read_text())["goal_state"], state)
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=5)
                proc.stdin.close()
                proc.stderr.close()

    def test_escape_stops_the_provider_and_preserves_fragmented_unicode_steering(self):
        import pty
        import select
        import signal
        import termios

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            fake = root / "fake-claude"
            fake.write_text(textwrap.dedent(f'''\
                #!{sys.executable}
                import json, pathlib, sys, time
                prompt = sys.argv[2]
                print(json.dumps({{"type": "system", "subtype": "init", "session_id": "interrupt-fixture"}}), flush=True)
                if prompt == "slow task":
                    pathlib.Path("provider.pid").write_text(str(__import__("os").getpid()))
                    time.sleep(30)
                    reply = "should have been interrupted"
                else:
                    pathlib.Path("steering.txt").write_text(prompt)
                    reply = "steering received"
                print(json.dumps({{"type": "stream_event", "event": {{"type": "content_block_delta",
                    "delta": {{"type": "text_delta", "text": reply}}}}}}), flush=True)
                print(json.dumps({{"type": "result", "session_id": "interrupt-fixture", "usage": {{}}}}), flush=True)
                '''), encoding="utf-8")
            fake.chmod(0o755)
            profiles = json.loads((ROOT / "agents.json").read_text())
            config = root / "config.json"
            config.write_text(json.dumps({"agents": {
                a["name"]: {"command": str(fake if a["name"] == "Claude" else root / "not-installed")}
                for a in profiles if a["name"] != "Star"
            }}))
            env = {k: v for k, v in os.environ.items() if not k.startswith(("HERDR_", "STAR_"))}
            env.update(AGENT_PICKER_STATE_HOME=str(root / "state"), AGENT_PICKER_CONFIG=str(config), NO_COLOR="1")
            driver = (f"import sys; sys.path.insert(0, {str(ROOT / 'src')!r}); import starcli; "
                      "starcli.refresh_meters = lambda *a, **k: None; "
                      "starcli.report_herdr = lambda *a, **k: None; sys.exit(starcli.main())")
            master, slave = pty.openpty()
            try:
                proc = subprocess.Popen([sys.executable, "-c", driver, "--use", "Claude", "slow task"],
                                        stdin=slave, stdout=slave, stderr=slave, cwd=root, env=env,
                                        start_new_session=True)
            finally:
                os.close(slave)
            output = bytearray()

            def pump():
                ready, _, _ = select.select([master], [], [], .05)
                if ready:
                    try:
                        output.extend(os.read(master, 65536))
                    except OSError:
                        pass

            def until(predicate, seconds=8):
                deadline = time.monotonic() + seconds
                while not predicate() and time.monotonic() < deadline:
                    self.assertIsNone(proc.poll(), output.decode(errors="replace"))
                    pump()
                self.assertTrue(predicate(), output.decode(errors="replace"))

            provider_pid = None
            try:
                until(lambda: (root / "provider.pid").exists()
                      and not termios.tcgetattr(master)[3] & termios.ICANON)
                provider_pid = int((root / "provider.pid").read_text())
                # The two UTF-8 bytes must arrive in separate OS reads.
                os.write(master, b"next caf\xc3")
                deadline = time.monotonic() + .15
                while time.monotonic() < deadline:
                    pump()
                os.write(master, b"\xa9")
                deadline = time.monotonic() + .15
                while time.monotonic() < deadline:
                    pump()
                os.write(master, b"\x1b")
                until(lambda: b"interrupted by" in output or b"interrupted" in output)
                until(lambda: b"\x1b[?2004h" in output)
                with self.assertRaises(ProcessLookupError):
                    os.kill(provider_pid, 0)
                # Submit the draft retained from KeyWatch in the real editor.
                os.write(master, b"\r")
                until(lambda: (root / "steering.txt").exists())
                self.assertEqual((root / "steering.txt").read_text(), "next café")
                until(lambda: b"steering received" in output)
                until(lambda: output.count(b"\x1b[?2004h") >= 2)
                os.write(master, b"/quit\r")
                deadline = time.monotonic() + 5
                while proc.poll() is None and time.monotonic() < deadline:
                    pump()
                self.assertEqual(proc.poll(), 0, output.decode(errors="replace"))
                saved = [json.loads(p.read_text()) for p in (root / "state/star/conversations").glob("*.json")]
                self.assertEqual(saved[0]["turns"][0]["status"], "interrupted")
                self.assertEqual(saved[0]["turns"][1]["user"], "next café")
            finally:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
                if provider_pid:
                    try:
                        os.killpg(provider_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                os.close(master)

    def test_three_ctrl_c_quit_with_a_draft(self):
        import pty
        import select
        import signal

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            profiles = json.loads((ROOT / "agents.json").read_text())
            config = root / "config.json"
            # A provider must exist for Star to start; the typed draft must never reach it.
            fake = root / "fake-claude"
            fake.write_text(f"#!{sys.executable}\nimport pathlib\npathlib.Path({str(root / 'ran')!r}).touch()\n")
            fake.chmod(0o755)
            config.write_text(json.dumps({"agents": {
                a["name"]: {"command": str(fake if a["name"] == "Claude" else root / "not-installed")}
                for a in profiles if a["name"] != "Star"}}))
            env = {k: v for k, v in os.environ.items() if not k.startswith(("HERDR_", "STAR_"))}
            env.update(AGENT_PICKER_STATE_HOME=str(root / "state"), AGENT_PICKER_CONFIG=str(config),
                       NO_COLOR="1", TERM="xterm-256color")
            master, slave = pty.openpty()
            proc = subprocess.Popen([sys.executable, str(ROOT / "src/starcli.py")], stdin=slave, stdout=slave,
                                    stderr=slave, cwd=root, env=env, start_new_session=True)
            os.close(slave)
            output = b""

            def pump(seconds):
                nonlocal output
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline and proc.poll() is None:
                    if select.select([master], [], [], .05)[0]:
                        try:
                            output += os.read(master, 65536)
                        except OSError:
                            return
            try:
                pump(3)
                self.assertIsNone(proc.poll(), output.decode(errors="replace")[-2000:])
                os.write(master, b"cd somewhere else")
                pump(.5)
                for _ in range(3):
                    os.write(master, b"\x03")
                    pump(.3)
                pump(3)
                self.assertEqual(proc.poll(), 0, output.decode(errors="replace")[-2000:])
                self.assertFalse((root / "ran").exists())
            finally:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
                os.close(master)

    def test_a_lead_delegates_to_an_idle_crew_and_integrates_its_file(self):
        self.exercise_mesh(crews=1, failover=False)

    def test_three_crews_work_concurrently_and_recover_partial_provider_failure(self):
        self.exercise_mesh(crews=3, failover=True)

    def exercise_mesh(self, crews, failover):
        import pty
        import select
        import signal
        import termios

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            script = textwrap.dedent(f'''\
                #!{sys.executable}
                import concurrent.futures, json, os, pathlib, subprocess, sys, time
                args = sys.argv[1:]
                who = os.environ["STAR_NAME"]
                codex = pathlib.Path(sys.argv[0]).name == "fake-codex"
                prompt = args[-1] if codex else args[1]
                with open({str(root / 'launches.jsonl')!r}, "a") as log:
                    log.write(json.dumps({{"who": who, "args": args, "provider": "Codex" if codex else "Claude"}}) + "\\n")
                if who == "lead":
                    def delegate(n):
                        result = subprocess.run([sys.executable, {str(ROOT / 'src/starcli.py')!r},
                            "--send", "crew" + str(n), "write sample" + str(n) + ".txt; preserve sentinel.txt",
                            "--wait", "--timeout", "12"], capture_output=True, text=True, check=True)
                        assert "wrote sample" + str(n) + ".txt" in result.stdout, result.stdout
                        assert pathlib.Path("sample" + str(n) + ".txt").read_text() == "crew work verified"
                    with concurrent.futures.ThreadPoolExecutor(max_workers={crews}) as pool:
                        list(pool.map(delegate, range(1, {crews + 1})))
                    assert pathlib.Path("sentinel.txt").read_text() == "preserved"
                    reply = "Integrated and verified every crew's file.\\nGOAL COMPLETE"
                else:
                    n = int(who[4:])
                    if not codex:
                        pathlib.Path(who + ".started").touch()
                        deadline = time.monotonic() + 8
                        while len(list(pathlib.Path('.').glob('crew*.started'))) < {crews}:
                            assert time.monotonic() < deadline, "crew turns did not overlap"
                            time.sleep(0.05)
                    if {failover!r} and who == "crew1" and not codex:
                        pathlib.Path("partial.txt").write_text("inspect before continuing")
                        print(json.dumps({{"type": "system", "subtype": "init", "session_id": "failed-crew1"}}))
                        print(json.dumps({{"type": "stream_event", "event": {{"type": "content_block_delta",
                            "delta": {{"type": "text_delta", "text": "Created partial.txt; task still unfinished."}}}}}}))
                        print(json.dumps({{"type": "result", "is_error": True, "result": "fixture quota exhausted"}}))
                        sys.exit(1)
                    if codex:
                        assert who == "crew1"
                        assert "partial.txt" in prompt and "preserve sentinel.txt" in prompt, prompt
                        assert "Inspect the current state" in prompt, prompt
                        assert pathlib.Path("partial.txt").read_text() == "inspect before continuing"
                    pathlib.Path("sample" + str(n) + ".txt").write_text("crew work verified")
                    reply = "wrote sample" + str(n) + ".txt"
                if codex:
                    print(json.dumps({{"type": "thread.started", "thread_id": "session-" + who}}))
                    print(json.dumps({{"type": "item.completed", "item": {{"type": "agent_message", "text": reply}}}}))
                    print(json.dumps({{"type": "turn.completed", "usage": {{}}}}))
                    sys.exit(0)
                print(json.dumps({{"type": "system", "subtype": "init", "session_id": "session-" + who}}))
                print(json.dumps({{"type": "stream_event", "event": {{"type": "content_block_delta",
                    "delta": {{"type": "text_delta", "text": reply}}}}}}))
                print(json.dumps({{"type": "result", "session_id": "session-" + who, "usage": {{}}}}))
                ''')
            for provider in ("claude", "codex"):
                fake = root / ("fake-" + provider)
                fake.write_text(script, encoding="utf-8")
                fake.chmod(0o755)
            (root / "sentinel.txt").write_text("preserved")
            profiles = json.loads((ROOT / "agents.json").read_text())
            config = root / "config.json"
            config.write_text(json.dumps({"agents": {
                a["name"]: {"command": str(root / ("fake-" + a["name"].lower())
                    if a["name"] == "Claude" or (failover and a["name"] == "Codex") else root / "not-installed")}
                for a in profiles if a["name"] != "Star"
            }}))
            env = {k: v for k, v in os.environ.items() if not k.startswith(("HERDR_", "STAR_"))}
            env.update(AGENT_PICKER_STATE_HOME=str(root / "state"), AGENT_PICKER_CONFIG=str(config),
                       HERDR_TAB_ID="test:tab", HERDR_WORKSPACE_ID="test", HERDR_SOCKET_PATH="/fixture/socket",
                       NO_COLOR="1")
            driver = (f"import sys, tempfile; sys.path.insert(0, {str(ROOT / 'src')!r}); "
                      f"tempfile.tempdir = {folder!r}; import starcli; "
                      "starcli.refresh_meters = lambda *a, **k: None; "
                      "starcli.report_herdr = lambda *a, **k: None; "
                      "config = starcli.star_router.load_config(); "
                      "config['Claude']['quality'] = dict.fromkeys(starcli.star_router.KINDS, 1); "
                      "config['Codex']['quality'] = dict.fromkeys(starcli.star_router.KINDS, 0); "
                      "starcli.star_router.load_config = lambda: config; sys.exit(starcli.main())")
            processes, terminals = [], []
            output = bytearray()

            def start(name, role, *args):
                master, slave = pty.openpty()
                try:
                    proc = subprocess.Popen([sys.executable, "-c", driver, "--name", name, "--role", role,
                                             *args], stdin=slave, stdout=slave, stderr=slave,
                                            cwd=root, env=env, start_new_session=True)
                finally:
                    os.close(slave)
                processes.append(proc)
                terminals.append(master)
                return proc

            def pump():
                ready, _, _ = select.select(terminals, [], [], 0.05)
                for fd in ready:
                    try:
                        output.extend(os.read(fd, 65536))
                    except OSError:
                        pass

            try:
                workers = [start("crew" + str(n), "crew") for n in range(1, crews + 1)]
                members = root / "state/star/mesh/members"
                deadline = time.monotonic() + 10
                while len(list(members.glob("crew*-*.json"))) < crews and time.monotonic() < deadline:
                    for crew in workers:
                        self.assertIsNone(crew.poll(), output.decode(errors="replace"))
                    pump()
                self.assertEqual(len(list(members.glob("crew*-*.json"))), crews, output.decode(errors="replace"))
                lead = start("lead", "lead", "--goal", "create and verify sample files with the crew; preserve sentinel.txt")
                deadline = time.monotonic() + 15
                while b"goal complete" not in output and time.monotonic() < deadline:
                    self.assertIsNone(lead.poll(), output.decode(errors="replace"))
                    pump()
                self.assertIn(b"goal complete", output, output.decode(errors="replace"))
                for n in range(1, crews + 1):
                    self.assertEqual((root / f"sample{n}.txt").read_text(), "crew work verified")
                self.assertEqual((root / "sentinel.txt").read_text(), "preserved")
                launches = [json.loads(line) for line in (root / "launches.jsonl").read_text().splitlines()]
                self.assertEqual(launches[0]["who"], "lead")
                self.assertEqual(len(launches), crews + 1 + int(failover))
                self.assertEqual({row["who"] for row in launches[1:]}, {f"crew{n}" for n in range(1, crews + 1)})
                self.assertIn("claude-opus-5-5", launches[0]["args"])
                self.assertIn("Task from lead", launches[1]["args"][1])
                saved = [json.loads(p.read_text()) for p in (root / "state/star/conversations").glob("*.json")]
                self.assertEqual(len(saved), crews + 1)
                self.assertTrue(any(c["sessions"].get("Claude") == "session-lead" and c["goal"] == "" for c in saved))
                worker = next(c for c in saved if c["sessions"].get("Codex" if failover else "Claude") == "session-crew1")
                self.assertEqual(next(iter(worker["mesh_tasks"].values()))["status"], "answered")
                if failover:
                    self.assertEqual(worker["sessions"]["Claude"], "failed-crew1")
                    self.assertEqual(worker["turns"][0]["status"], "failed")
                    self.assertEqual(worker["turns"][1]["provider"], "Codex")
                self.assertFalse(list((root / "state/star/mesh/inbox").rglob("*.json")))
                # A provider's last line can precede the editor entering raw mode.
                # Sending Enter while the tty is canonical would turn it into a newline.
                deadline = time.monotonic() + 5
                while any(termios.tcgetattr(fd)[3] & termios.ICANON for fd in terminals) and time.monotonic() < deadline:
                    pump()
                self.assertTrue(all(not termios.tcgetattr(fd)[3] & termios.ICANON for fd in terminals))
                for fd in terminals:
                    os.write(fd, b"/quit\r")
                deadline = time.monotonic() + 5
                while any(proc.poll() is None for proc in processes) and time.monotonic() < deadline:
                    pump()
                for proc in processes:
                    self.assertEqual(proc.poll(), 0, output.decode(errors="replace"))
            finally:
                for proc in processes:
                    if proc.poll() is None:
                        os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=5)
                for fd in terminals:
                    os.close(fd)
