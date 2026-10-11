"""Opt-in Unix smoke: python3 tests/native_star_herdr.py.

Requires an existing Herdr-managed caller. Uses one owned named server with
fake providers and private config/state. Never submits work to vendor CLIs.
The startup-hook installer is excluded because it changes the shared plugin
registry. Real layout, picker, usage, sidebar metadata and Star reporting run.
Artifacts remain in a printed temporary directory for inspection.
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


def main():
    if os.environ.get("HERDR_ENV") != "1":
        raise SystemExit("Run this Unix smoke from a Herdr-managed pane.")
    binary = shutil.which("herdr")
    if not binary:
        raise SystemExit("Herdr is not installed.")
    root = Path(tempfile.mkdtemp(prefix="starcli-native-"))
    name = "starcli-test-" + str(os.getpid())
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HERDR_", "STAR_"))}
    env.update(AGENT_PICKER_CONFIG=str(root / "picker.json"), AGENT_PICKER_STATE_HOME=str(root / "state"),
               HERDR_CONFIG_PATH=str(root / "config.toml"), NO_COLOR="1", SHELL="/bin/sh")
    (root / "config.toml").write_text(
        'onboarding = false\n[terminal]\ndefault_shell = "/bin/sh"\nshell_mode = "non_login"\n')
    fake = root / "fake-claude"
    fake.write_text(f"#!{sys.executable}\n" + '''
import json, os, pathlib, subprocess, sys, time
prompt = sys.argv[2]
with open("launches.jsonl", "a") as log:
    log.write(json.dumps({"name": os.environ.get("STAR_NAME"), "args": sys.argv[1:]}) + "\\n")
if "hold fixture turn" in prompt:
    deadline = time.monotonic() + 20
    while not pathlib.Path("release-provider").exists():
        assert time.monotonic() < deadline
        time.sleep(.05)
else:
    time.sleep(1.5)
reply = "GOAL BLOCKED: fixture needs user input" if "fixture blocked objective" in prompt else "native fixture verified"
if os.environ["STAR_NAME"] == "lead" and "delegate native fixture" in prompt:
    result = subprocess.run([sys.executable, os.environ["STAR_FIXTURE_CLI"], "--send", "crew2",
                             "verify a delegated native fixture", "--wait", "--timeout", "12"],
                            capture_output=True, text=True, check=True)
    assert "native fixture verified" in result.stdout, result.stdout
    reply = "native delegation integrated"
print(json.dumps({"type": "system", "subtype": "init", "session_id": "fake-" + os.environ["STAR_NAME"]}))
event = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": reply}}
print(json.dumps({"type": "stream_event", "event": event}))
print(json.dumps({"type": "result", "session_id": "fake-" + os.environ["STAR_NAME"], "usage": {}}))
''')
    fake.chmod(0o755)
    env["STAR_FIXTURE_CLI"] = str(ROOT / "src/starcli.py")
    profiles = json.loads((ROOT / "agents.json").read_text())
    overrides = {a["name"]: {"command": str(fake if a["name"] == "Claude" else root / "not-installed")}
                 for a in profiles if a["name"] != "Star"}
    overrides["Star"] = {"argv": [sys.executable, str(ROOT / "src/starcli.py")], "executables": []}
    (root / "picker.json").write_text(json.dumps({"agents": overrides}))
    setup = root / "setup.py"
    setup.write_text("import sys\nsys.path.insert(0, " + repr(str(ROOT / "src")) + ")\n"
                     "import herdr_ai\nherdr_ai.herdr_sidebar.install_startup = lambda *a, **k: None\n"
                     "sys.exit(herdr_ai.main(['--setup', 'star']))\n")
    prefix = [binary, "--session", name]
    socket_path = None

    def cli(*args):
        result = subprocess.run([*prefix, *args], env=env, capture_output=True, text=True, timeout=25)
        if result.returncode:
            raise AssertionError(result.stderr or result.stdout)
        return json.loads(result.stdout)["result"] if result.stdout.strip() else {}

    def wait(predicate, seconds=15):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(.1)
        raise AssertionError("Timed out waiting for native fixture state; inspect " + str(root))

    def agents():
        return {a["pane_id"]: a for a in cli("api", "snapshot")["snapshot"]["agents"]}

    def members():
        found = []
        for path in (root / "state/star/mesh/members").glob("*.json"):
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if data.get("socket") == socket_path:
                try:
                    os.kill(data["pid"], 0)
                except ProcessLookupError:
                    continue
                found.append(data)
        # Killed wrappers can leave old member files until the next peer read.
        # Only this isolated fixture observes its newest join for each name.
        return {m["name"]: m for m in sorted(found, key=lambda m: m["joined"])}

    log = (root / "server.log").open("w")
    server = subprocess.Popen([*prefix, "server"], env=env, stdin=subprocess.DEVNULL, stdout=log,
                              stderr=subprocess.STDOUT, start_new_session=True)
    try:
        def ready():
            if server.poll() is not None:
                raise AssertionError((root / "server.log").read_text())
            match = re.search(r"api socket: (.+)", (root / "server.log").read_text())
            return match[1].strip() if match else None
        socket_path = wait(ready)
        assert name in socket_path
        workspace = cli("workspace", "create", "--cwd", str(root), "--label", "StarCLI native test", "--no-focus")
        terminal = workspace["root_pane"]["pane_id"]
        wid = workspace["workspace"]["workspace_id"]
        setup_line = shlex.join([sys.executable, str(setup)])
        cli("pane", "run", terminal, setup_line)
        wait(lambda: len(members()) == 5 and len(agents()) == 5 and all(m.get("ready") for m in members().values()))
        before = members()
        panes = cli("pane", "list", "--workspace", wid)["panes"]
        assert len(panes) == 7
        # A workspace's custom env does not carry into split panes. These checks
        # require the fixture config inherited from the named server itself.
        for pane in panes:
            if pane.get("agent") != "star":
                continue
            info = cli("pane", "process-info", "--pane", pane["pane_id"])["process_info"]
            for process in info["foreground_processes"]:
                text = subprocess.check_output(["ps", "eww", "-p", str(process["pid"])], text=True)
                assert "AGENT_PICKER_CONFIG=" + str(root / "picker.json") in text
        lead, crew, quitting = (before[key]["pane"] for key in ("lead", "crew1", "crew4"))
        cli("pane", "run", lead, "delegate native fixture")
        cli("pane", "wait-output", lead, "--match", "native delegation integrated", "--timeout", "15000")
        wait(lambda: members()["lead"].get("ready") and members()["crew2"].get("ready"))
        # Keep a real provider turn alive while refresh examines the grid.
        cli("pane", "run", crew, "hold fixture turn")
        wait(lambda: agents()[crew]["agent_status"] == "working")
        wait(lambda: agents()[lead].get("tokens", {}).get("herdr_ai_status") == "working")
        cli("pane", "run", terminal, setup_line)
        cli("pane", "wait-output", terminal, "--match", "kept crew1", "--timeout", "15000")
        assert members()["crew1"]["pid"] == before["crew1"]["pid"]
        wait(lambda: len(members()) == 5 and all(members()[key]["pid"] != old["pid"]
                                                for key, old in before.items() if key != "crew1"))
        (root / "release-provider").touch()
        wait(lambda: agents()[crew]["agent_status"] == "idle" and agents()[crew].get("completion_seq"))
        wait(lambda: len(members()) == 5)
        # Once idle, every exact conversation must survive a complete refresh.
        before = members()
        cli("pane", "run", terminal, setup_line)
        wait(lambda: len(members()) == 5 and all(
            members()[key]["pid"] != old["pid"] and members()[key].get("ready") for key, old in before.items()))
        after = members()
        for key, old in before.items():
            assert (after[key]["conversation"], after[key]["pane"], after[key]["role"]) == (
                old["conversation"], old["pane"], old["role"])
        refreshed = cli("pane", "list", "--workspace", wid)["panes"]
        assert {p["pane_id"]: p["terminal_id"] for p in panes} == {
            p["pane_id"]: p["terminal_id"] for p in refreshed}
        # Blocked goals remain visible and are retained by refresh.
        cli("pane", "run", lead, "/goal fixture blocked objective")
        wait(lambda: agents()[lead]["agent_status"] == "blocked")
        blocked = members()["lead"]
        others = members()
        cli("pane", "run", terminal, setup_line)
        cli("pane", "wait-output", terminal, "--match", "kept lead", "--timeout", "15000")
        assert members()["lead"]["pid"] == blocked["pid"]
        wait(lambda: len(members()) == 5 and all(
            members()[key]["pid"] != old["pid"] and members()[key].get("ready")
            for key, old in others.items() if key != "lead"))
        cli("pane", "run", lead, "/goal clear")
        wait(lambda: agents()[lead]["agent_status"] == "idle")
        wait(lambda: members()["crew4"].get("ready"))
        # A typed draft is also live work and must survive a refresh.
        cli("pane", "send-text", quitting, "keep this draft")
        wait(lambda: members()["crew4"].get("draft"))
        drafting = members()
        cli("pane", "run", terminal, setup_line)
        cli("pane", "wait-output", terminal, "--match", "kept crew4", "--timeout", "15000")
        assert members()["crew4"]["pid"] == drafting["crew4"]["pid"]
        result = subprocess.run([*prefix, "pane", "read", quitting], env=env, capture_output=True, text=True, check=True)
        assert "keep this draft" in result.stdout
        cli("pane", "send-keys", quitting, "ctrl+u")
        wait(lambda: not members()["crew4"].get("draft"))
        wait(lambda: len(members()) == 5 and all(
            members()[key]["pid"] != old["pid"] and members()[key].get("ready")
            for key, old in drafting.items() if key != "crew4"))
        cli("pane", "run", quitting, "/quit")
        wait(lambda: "crew4" not in members())
        wait(lambda: not any("starcli.py" in p.get("cmdline", "") for p in
                            cli("pane", "process-info", "--pane", quitting)["process_info"]["foreground_processes"]))
        info = cli("pane", "process-info", "--pane", quitting)["process_info"]
        assert any("agent_picker.py" in p.get("cmdline", "") for p in info["foreground_processes"])
        assert not any("starcli.py" in p.get("cmdline", "") for p in info["foreground_processes"])
        evidence = {"session": name, "before": before, "after": after,
                    "snapshot": cli("api", "snapshot")["snapshot"],
                    "launches": [json.loads(line) for line in (root / "launches.jsonl").read_text().splitlines()]}
        (root / "evidence.json").write_text(json.dumps(evidence, indent=2))
        print("PASS: grid, delegation, working-crew refresh, exact idle resume, blocked goal, draft protection, sidebar lifecycle, picker return")
    except BaseException:
        try:
            (root / "failure-snapshot.json").write_text(json.dumps(cli("api", "snapshot"), indent=2))
            (root / "failure-members.json").write_text(json.dumps(members(), indent=2))
            for pane in cli("pane", "list", "--workspace", wid)["panes"]:
                result = subprocess.run([*prefix, "pane", "read", pane["pane_id"], "--source", "recent-unwrapped"],
                                        env=env, capture_output=True, text=True, timeout=5)
                (root / (pane["pane_id"].replace(":", "-") + ".txt")).write_text(result.stdout)
        except Exception:
            pass
        raise
    finally:
        try:
            subprocess.run([binary, "session", "stop", name, "--json"], env=env, capture_output=True, timeout=10)
            server.wait(timeout=10)
        finally:
            if server.poll() is None:
                os.killpg(server.pid, signal.SIGTERM)
                server.wait(timeout=5)
            log.close()
            # Startup hooks can start detached observers. Stop only this socket's.
            if socket_path:
                for row in subprocess.check_output(["ps", "-axo", "pid=,command="], text=True).splitlines():
                    parts = row.strip().split(None, 1)
                    if len(parts) != 2 or not parts[1].endswith("herdr_ai.py --watch-sidebar"):
                        continue
                    pid = int(parts[0])
                    result = subprocess.run(["ps", "eww", "-p", str(pid)], capture_output=True, text=True)
                    marker = "HERDR_SOCKET_PATH=" + socket_path
                    if marker + " " in result.stdout or result.stdout.rstrip().endswith(marker):
                        try:
                            os.kill(pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass
            print("Artifacts:", root)


if __name__ == "__main__":
    main()
