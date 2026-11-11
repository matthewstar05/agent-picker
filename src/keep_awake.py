"""Keep the Mac awake through Amphetamine while an agent is working.

With Usage panel › Keep awake while agents work on, an Amphetamine session runs
whenever an agent is working, so turning the display off or closing the lid
doesn't put the Mac to sleep and pause it. The display may still sleep; only
the system stays awake. The session ends a minute after the last agent
stops working, or as soon as the setting is turned off.

An agent counts as working when Herdr reports one of its panes as working, a
StarCLI or Grok Bot turn is running, or a scheduled prompt is running. The
agent selector, the usage panel and the scheduler's background dispatcher all
call tick(); a lock and one shared state file keep them from doubling up, so
it works while any of them is running.

Only a session this module started is ever ended. A session you start yourself,
or one a Trigger starts, is left alone. If our session ends while agents are
still working (you ended it, or Amphetamine ended it on low battery), it isn't
restarted until the agents have gone idle and started again.

macOS only: on other systems the setting is not offered and tick() does nothing.
"""

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

import preferences
from platform_runtime import state_dir

SUPPORTED = sys.platform == "darwin"
STATE = state_dir() / "keep-awake.json"
LOCK = state_dir() / "keep-awake.lock"
HERDR = os.environ.get("HERDR_BIN_PATH") or "herdr"
APP = "Amphetamine"
APP_PATHS = (Path("/Applications/Amphetamine.app"), Path.home() / "Applications/Amphetamine.app")
INTERVAL = 10        # seconds between looks at the agents, across every process that ticks
GRACE = 60           # seconds kept awake after the last working agent, to bridge gaps between turns
_service_checked = False   # whether this process has made sure the dispatcher is installed

KEEP_AWAKE = preferences.register(preferences.Setting(
    "usage.keep_awake", "Usage panel", "Keep awake while agents work", [False, True], False,
    hint="Amphetamine keeps the Mac awake while agents work, lid shut too.",
    names={False: "off", True: "on"})) if SUPPORTED else None


def enabled():
    return SUPPORTED and preferences.get(KEEP_AWAKE.key) is True


def installed():
    return any(path.exists() for path in APP_PATHS)


# ---- what's working ---------------------------------------------------------

def _live_turns(folder):
    """The pid of a live-turn file in folder whose process is still running, or None."""
    from star_mesh import alive
    for path in folder.glob("*.json") if folder.is_dir() else ():
        try:
            pid = int(json.loads(path.read_text(encoding="utf-8"))["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if alive(pid):
            return pid
    return None


def _herdr_working():
    try:
        out = subprocess.run([HERDR, "pane", "list"], capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=5)
        panes = json.loads(out.stdout)["result"]["panes"]
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return []
    return [f"{p.get('agent')} in Herdr pane {p.get('pane_id')}" for p in panes
            if p.get("agent") and p.get("agent_status") == "working"]


def _scheduled_running():
    try:
        import prompt_scheduler
        return [job["id"] for job in prompt_scheduler.read()["jobs"] if job.get("running")]
    except Exception:
        return []


def _starployment_working():
    """Native Starployment processes with JARVIS enabled in their open database.

    Match the process first, then inspect only the database path reported by lsof
    for that PID. This avoids keeping the Mac awake for an old database or a
    different Starployment checkout. The SQLite connection is strictly read-only
    and selects only the two settings needed here.
    """
    if not SUPPORTED:
        return []
    try:
        result = subprocess.run(["ps", "-axo", "pid=,comm="], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=5)
        if getattr(result, "returncode", 0):
            return []
    except (OSError, subprocess.SubprocessError):
        return []

    found = []
    seen = set()
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) != 2:
            continue
        try:
            pid = int(fields[0])
        except ValueError:
            continue
        name = Path(fields[1]).name.casefold()
        if name not in {"starployment", "jobhub"} or pid in seen:
            continue
        seen.add(pid)
        try:
            opened = subprocess.run(["lsof", "-Fn", "-p", str(pid)], capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=5)
            if getattr(opened, "returncode", 0):
                continue
        except (OSError, subprocess.SubprocessError):
            continue
        paths = [line[1:] for line in opened.stdout.splitlines()
                 if line.startswith("n") and Path(line[1:]).name == "jobhub.db"]
        for database in paths:
            try:
                uri = "file:" + quote(str(Path(database).resolve()), safe="/") + "?mode=ro"
                connection = sqlite3.connect(uri, uri=True, timeout=1)
                try:
                    rows = dict(connection.execute(
                        "SELECT key, value FROM settings WHERE key IN ('jarvis_mode', 'setup_complete')"))
                finally:
                    connection.close()
                mode = json.loads(rows.get("jarvis_mode", '"off"'))
                setup = json.loads(rows.get("setup_complete", "false"))
            except (OSError, sqlite3.Error, ValueError, TypeError):
                continue
            if setup is True and mode in ("new", "all", "all_plus"):
                label = "JobHub" if name == "jobhub" else "Starployment"
                found.append(f"{label} JARVIS (pid {pid})")
                break
    return found


def agents_working():
    """What is working right now, as short descriptions; empty when nothing is."""
    base = state_dir()
    found = [f"{name} turn (pid {pid})" for name, pid in
             (("StarCLI", _live_turns(base / "star" / "live")), ("Grok Bot", _live_turns(base / "grokbot" / "live"))) if pid]
    found += [f"scheduled prompt {job}" for job in _scheduled_running()]
    return found + _starployment_working() + _herdr_working()


# ---- Amphetamine ------------------------------------------------------------

def _tell(command):
    """Run one Amphetamine AppleScript command; its result as text, or None if it failed."""
    try:
        out = subprocess.run(["osascript", "-e", f'tell application "{APP}" to {command}'],
                             capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def session_active():
    """True or False, or None when Amphetamine can't be asked."""
    answer = _tell("session is active")
    return None if answer is None else answer == "true"


def ours():
    """Whether the running session looks like the one start() began: no end time, no Trigger.
    A timed session you started over it is yours, and is left alone. None when
    Amphetamine couldn't be asked."""
    remaining, trigger = _tell("session time remaining"), _tell("session is Trigger")
    if remaining is None or trigger is None:
        return None
    return remaining == "0" and trigger == "false"


def start():
    """An open-ended session that lets the display sleep but keeps the system awake,
    including with the lid closed."""
    if _tell("start new session with options {duration:0, interval:0, displaySleepAllowed:true}") is None:
        return False
    if _tell("closed display mode enabled") == "false":
        _tell("enable closed display mode")
    return True


def end():
    return _tell("end session") is not None


# ---- the tick ---------------------------------------------------------------

def _read():
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(data):
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE.with_name(f".keep-awake.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, STATE)
    except OSError:
        pass


@contextmanager
def _locked():
    """Held, or None when another process is ticking right now."""
    import fcntl
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield None
            return
        try:
            yield handle
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def tick(now=None, force=False):
    """Start or end our Amphetamine session to match the agents. Cheap to call often:
    it looks at the agents at most every INTERVAL seconds across every process.
    Returns the saved state, or None when it did nothing."""
    if not SUPPORTED:
        return None
    now = time.time() if now is None else now
    on = enabled()
    if not on and not _read().get("owned"):
        return None   # off, with no session of ours to end: not even the lock
    try:
        with _locked() as held:
            if held is None:
                return None
            state = _read()
            if not on and not state.get("owned"):
                return None
            if not force and on and now - state.get("checked", 0) < INTERVAL:
                return None
            state["checked"] = now
            global _service_checked
            if on and not _service_checked:
                _service_checked = True
                threading.Thread(target=ensure_service, daemon=True).start()
            working = agents_working() if on else []
            state["reason"] = working[:5]   # what kept the Mac awake last, for anyone wondering why
            if working:
                state["working"] = now
            wanted = on and now - state.get("working", 0) < GRACE
            if wanted and installed():
                if state.get("owned"):
                    if session_active() is False:
                        # Ended under us (by you, or on low battery): respect it for this stretch.
                        state.update(owned=False, dismissed=True)
                elif not state.get("dismissed") and session_active() is False and start():
                    state.update(owned=True, started=now)
                # An active session that isn't ours is yours; nothing to do.
            elif not wanted:
                state["dismissed"] = False
                if state.get("owned"):
                    active = session_active()
                    mine = ours() if active else None
                    if active is False or mine is False:
                        state["owned"] = False   # already over, or replaced by a session of yours
                    elif mine and end():
                        state["owned"] = False
                    # Otherwise Amphetamine couldn't be asked or told: keep it and try again next look.
            _write(state)
            return state
    except OSError:
        return None


def ensure_service():
    """Make sure the scheduler's background dispatcher is installed, so the session
    follows the agents even after the selector and usage panel close."""
    if not enabled():
        return
    try:
        import prompt_scheduler
        if not prompt_scheduler.service_status():
            prompt_scheduler.install_service()
    except Exception:
        pass

