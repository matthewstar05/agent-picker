"""Opt-in Unix smoke: python3 tests/native_fs_herdr.py [--keep] [--shot DIR].

Requires an existing Herdr-managed caller. Uses one owned named server with fake
Claude panes and private config, state and Claude config. herdr-ai lays out a
five-agent Claude tab with the system prompt off; the bottom-left pane must
become the fs file browser in icon view. Tab+Enter then 1 must assign only
the top-left agent without browsing into the folder, and Shift+Enter must
restart every agent pane in that folder. Never starts a vendor CLI. Herdr's
plugin registry is left alone (a wrapper turns `herdr plugin` into a no-op).
"""

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main(argv):
    if os.environ.get("HERDR_ENV") != "1":
        raise SystemExit("Run this Unix smoke from a Herdr-managed pane.")
    real = shutil.which("herdr")
    if not real:
        raise SystemExit("Herdr is not installed.")
    root = Path(tempfile.mkdtemp(prefix="fs-native-")).resolve()
    work, project = root / "work", root / "work" / "project"
    project.mkdir(parents=True)
    (work / "notes.md").write_text("# notes\n")
    name = "fs-test-" + str(os.getpid())
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HERDR_", "STAR_"))}
    wrapper = root / "herdr"
    wrapper.write_text(f'#!/bin/sh\nif [ "$1" = plugin ]; then echo \'{{"result":{{}}}}\'; exit 0; fi\n'
                       f'exec {shlex.quote(real)} "$@"\n')
    wrapper.chmod(0o755)
    env.update(AGENT_PICKER_CONFIG=str(root / "picker.json"), AGENT_PICKER_STATE_HOME=str(root / "state"),
               HERDR_CONFIG_PATH=str(root / "config.toml"), HERDR_BIN_PATH=str(wrapper),
               CLAUDE_CONFIG_DIR=str(root / "claude"), AGENT_PICKER_NO_PRETRUST="1", SHELL="/bin/sh")
    (root / "config.toml").write_text(
        'onboarding = false\n[terminal]\ndefault_shell = "/bin/sh"\nshell_mode = "non_login"\n')
    (root / "state").mkdir()
    (root / "state" / "preferences.json").write_text(json.dumps(
        {"fs.start": "herdr", "usage.keep_awake": False, "herdr_ai.system_prompt": "off"}))
    fake = root / "fake-claude"
    fake.write_text(f"#!{sys.executable}\n" + f'''
import json, os, time
with open({str(root / "launches.jsonl")!r}, "a") as log:
    log.write(json.dumps({{"cwd": os.getcwd(), "pane": os.environ.get("HERDR_PANE_ID")}}) + "\\n")
print("fake claude in", os.getcwd(), flush=True)
while True:
    time.sleep(1)
''')
    fake.chmod(0o755)
    profiles = json.loads((ROOT / "agents.json").read_text())
    (root / "picker.json").write_text(json.dumps({"agents": {
        a["name"]: {"command": str(fake if a["name"] == "Claude" else root / "not-installed")} for a in profiles}}))
    prefix = [real, "--session", name]

    def cli(*args):
        result = subprocess.run([*prefix, *args], env=env, capture_output=True, text=True, timeout=25)
        if result.returncode:
            raise AssertionError(result.stderr or result.stdout)
        return json.loads(result.stdout)["result"] if result.stdout.strip() else {}

    def wait(predicate, seconds=30, what="state"):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(.2)
        raise AssertionError(f"Timed out waiting for {what}; inspect {root}")

    def screen(pane, ansi=False):
        args = ["pane", "read", pane, "--source", "visible"] + (["--format", "ansi"] if ansi else [])
        result = subprocess.run([*prefix, *args], env=env, capture_output=True, text=True, timeout=25)
        return result.stdout

    def foreground(pane):
        info = cli("pane", "process-info", "--pane", pane)["process_info"]
        return " ".join(" ".join(p.get("argv") or [p.get("name", "")]) for p in info.get("foreground_processes", []))

    def launches():
        path = root / "launches.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    log = (root / "server.log").open("w")
    server = subprocess.Popen([*prefix, "server"], env=env, stdin=subprocess.DEVNULL, stdout=log,
                              stderr=subprocess.STDOUT, start_new_session=True)
    socket_path = None
    try:
        def ready():
            if server.poll() is not None:
                raise AssertionError((root / "server.log").read_text())
            match = re.search(r"api socket: (.+)", (root / "server.log").read_text())
            return match[1].strip() if match else None
        socket_path = wait(ready, what="the server")
        workspace = cli("workspace", "create", "--cwd", str(work), "--label", "fs native test", "--no-focus")
        terminal = workspace["root_pane"]["pane_id"]
        setup = root / "setup.py"
        setup.write_text("import sys\nsys.path.insert(0, " + repr(str(ROOT / "src")) + ")\n"
                         "import herdr_ai\nsys.exit(herdr_ai.main(['--setup', 'claude', '--mode', 'off']))\n")
        cli("pane", "run", terminal, shlex.join([sys.executable, str(setup)]))
        wait(lambda: "fs_browser.py --herdr --view icon" in foreground(terminal), what="fs in the terminal pane")
        wait(lambda: "project" in screen(terminal) and "✦" in screen(terminal), what="the browser's first frame")
        wait(lambda: len(launches()) >= 5, what="five fake Claude panes")
        first = screen(terminal)
        assert "work" in first.splitlines()[0], first
        before = len(launches())
        assert all(Path(l["cwd"]).resolve() == work for l in launches()), launches()
        shots = argv[argv.index("--shot") + 1] if "--shot" in argv else None
        if shots:
            Path(shots, "native-before.ansi").write_text(screen(terminal, ansi=True))
        # Feed kitty Tab press + Enter, then release. No physical keyboard state required.
        cli("pane", "send-text", terminal, "\x1b[9u\x1b[13u\x1b[9;1:3u")
        wait(lambda: "assign directory" in screen(terminal), what="the panel assignment menu")
        cli("pane", "send-keys", terminal, "1")
        wait(lambda: len(launches()) == before + 1, seconds=60, what="only the selected agent to restart")
        wait(lambda: "panel 1 assigned" in screen(terminal), what="the assignment report")
        entries = json.loads((root / "state" / "herdr-ai-tabs.json").read_text())
        layout = next(entry for entry in entries.values() if entry["panes"]["terminal"] == terminal)
        assigned = launches()[before:]
        assert len(assigned) == 1 and assigned[0]["pane"] == layout["panes"]["crew1"], assigned
        assert Path(assigned[0]["cwd"]).resolve() == project, assigned
        assigned_screen = screen(terminal)
        assert "work" in assigned_screen.splitlines()[0], assigned_screen
        assert "notes.md" in assigned_screen, assigned_screen
        before = len(launches())
        # project stays selected: Shift+Enter now moves all agents and follows them.
        cli("pane", "send-keys", terminal, "shift+enter")
        wait(lambda: len(launches()) >= before + 4, seconds=60, what="the agents to restart")
        moved = launches()[before:]
        assert all(Path(l["cwd"]).resolve() == project for l in moved), moved
        wait(lambda: "now in project" in screen(terminal), what="the browser's report")
        after = screen(terminal)
        assert "project" in after.splitlines()[0], after   # the browser followed the agents
        if shots:
            Path(shots, "native-after.ansi").write_text(screen(terminal, ansi=True))
        cli("pane", "send-keys", terminal, "esc")   # back to the work folder, still browsing
        wait(lambda: "work" in screen(terminal).splitlines()[0] and "project" not in screen(terminal).splitlines()[0],
             what="Esc to go back a folder")
        cli("pane", "send-keys", terminal, "q")
        time.sleep(1)
        assert "fs_browser.py" in foreground(terminal), "q must not quit the browser"
        cli("pane", "send-keys", terminal, "shift+q")
        wait(lambda: "fs_browser.py" not in foreground(terminal), what="Shift+Q to leave the browser")
        print(after)
        print("Native fs smoke passed:", root)
    except Exception:
        try:
            for pane in cli("pane", "list")["panes"]:
                (root / (pane["pane_id"].replace(":", "-") + ".txt")).write_text(screen(pane["pane_id"]))
        except Exception:
            pass
        raise
    finally:
        try:
            subprocess.run([real, "session", "stop", name, "--json"], env=env, capture_output=True, timeout=10)
            server.wait(timeout=10)
        finally:
            if server.poll() is None:
                os.killpg(server.pid, signal.SIGTERM)
                server.wait(timeout=5)
            log.close()
            if socket_path:   # startup hooks can start detached observers; stop only this socket's
                for row in subprocess.check_output(["ps", "-axo", "pid=,command="], text=True).splitlines():
                    parts = row.strip().split(None, 1)
                    if len(parts) != 2 or "herdr_ai.py" not in parts[1]:
                        continue
                    result = subprocess.run(["ps", "eww", "-p", parts[0]], capture_output=True, text=True)
                    if "HERDR_SOCKET_PATH=" + socket_path in result.stdout:
                        try:
                            os.kill(int(parts[0]), signal.SIGTERM)
                        except ProcessLookupError:
                            pass
            if "--keep" not in argv:
                print("Artifacts:", root)


if __name__ == "__main__":
    main(sys.argv[1:])
