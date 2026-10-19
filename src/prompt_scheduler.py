"""Private local schedules and a sleeping, launchd-managed prompt dispatcher."""

import argparse
import calendar
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta
import fcntl
import json
import os
from pathlib import Path
import plistlib
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from zoneinfo import ZoneInfo

from platform_runtime import resolve_argv, state_dir, SEARCH_DIRS
from settings import load_agents

PROVIDERS = ("Claude", "Codex", "Gemini")
REPEATS = ("once", "hourly", "daily", "weekdays", "weekly", "monthly")
LABEL = "com.agent-picker.scheduler"
POLL_SECONDS = 60
MAX_WORKERS = 3


def directory():
    path = state_dir() / "scheduler"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def schedule_path():
    return directory() / "schedules.json"


def local_zone():
    name = os.environ.get("TZ", "").lstrip(":")
    if not name:
        target = str(Path("/etc/localtime").resolve())
        if "zoneinfo/" in target:
            name = target.split("zoneinfo/", 1)[1]
    try:
        return ZoneInfo(name or "UTC").key
    except (ValueError, KeyError):
        return "UTC"


def wall_time(day, clock, zone):
    """Resolve a local clock time; reject DST gaps and choose the first fold."""
    value = datetime.combine(day, clock).replace(tzinfo=ZoneInfo(zone), fold=0)
    check = datetime.fromtimestamp(value.timestamp(), ZoneInfo(zone))
    if check.replace(tzinfo=None) != value.replace(tzinfo=None):
        raise ValueError("That local time does not exist after a daylight-saving change.")
    return value.timestamp()


def parse_time(text):
    text = text.strip().upper()
    if re.fullmatch(r"\d{4}", text):
        text = text[:2] + ":" + text[2:]
    for fmt in ("%H:%M", "%I:%M %p", "%I:%M%p", "%I %p", "%I%p"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            pass
    raise ValueError("Use HH:MM, 1430, or 2:30 PM.")


def next_occurrence(job, after):
    """Coalesce missed repeats; retain wall clock time and original month day."""
    if job["repeat"] == "once":
        return None
    anchor = datetime.fromisoformat(job["anchor"])
    zone = ZoneInfo(job["timezone"])
    local = datetime.fromtimestamp(after, zone)
    if job["repeat"] == "hourly":
        first = wall_time(anchor.date(), anchor.time(), job["timezone"])
        return first + (max(0, int((after - first) // 3600)) + 1) * 3600
    day = max(anchor.date(), local.date())
    if job["repeat"] == "monthly":
        year, month = day.year, day.month
        for _ in range(24):
            candidate = day.replace(year=year, month=month,
                                    day=min(anchor.day, calendar.monthrange(year, month)[1]))
            try:
                epoch = wall_time(candidate, anchor.time(), job["timezone"])
                if epoch > after:
                    return epoch
            except ValueError:
                pass
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    else:
        for offset in range(370):
            candidate = day + timedelta(days=offset)
            if job["repeat"] == "weekdays" and candidate.weekday() >= 5:
                continue
            if job["repeat"] == "weekly" and candidate.weekday() != anchor.weekday():
                continue
            try:
                epoch = wall_time(candidate, anchor.time(), job["timezone"])
                if epoch > after:
                    return epoch
            except ValueError:
                continue
    raise ValueError("Could not find the next valid occurrence.")


@contextmanager
def lock(name, blocking=True):
    path = directory() / name
    with open(path, "a+", encoding="utf-8") as handle:
        os.chmod(path, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield None
            return
        try:
            yield handle
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def read():
    path = schedule_path()
    if not path.exists():
        return {"version": 1, "jobs": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("jobs"), list):
        raise ValueError(f"Invalid schedule file: {path}. Kept intact.")
    ids = set()
    for job in data["jobs"]:
        try:
            if not isinstance(job, dict):
                raise ValueError("entry must be an object")
            valid = (isinstance(job["id"], str) and re.fullmatch(r"[a-f0-9]{32}", job["id"]) and job["id"] not in ids
                     and job["provider"] in PROVIDERS and job["repeat"] in REPEATS
                     and isinstance(job["prompt"], str) and job["prompt"].strip()
                     and isinstance(job["cwd"], str) and Path(job["cwd"]).is_absolute()
                     and type(job["enabled"]) is bool and type(job.get("running", False)) is bool
                     and (job.get("last") is None or isinstance(job["last"], dict))
                     and (not job.get("running") or isinstance(job.get("last"), dict))
                     and (job["next_at"] is None or
                          (type(job["next_at"]) in (int, float) and 0 < job["next_at"] < 253402300799)))
            if not valid or datetime.fromisoformat(job["anchor"]).tzinfo is not None:
                raise ValueError("invalid schedule fields")
            ZoneInfo(job["timezone"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid schedule entry in {path}. Kept intact.") from exc
        ids.add(job["id"])
    return data


def write(data):
    path = schedule_path()
    fd, name = tempfile.mkstemp(prefix=".schedules-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        folder = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(folder)
        finally:
            os.close(folder)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def mutate(change):
    with lock("store.lock"):
        data = read()
        result = change(data["jobs"])
        write(data)
        return result


def save_job(values, job_id=None, now=None):
    now = time.time() if now is None else now
    job = deepcopy(values)
    if job.get("provider") not in PROVIDERS or job.get("repeat") not in REPEATS:
        raise ValueError("Choose a supported CLI and frequency.")
    if not job.get("prompt", "").strip():
        raise ValueError("Write a prompt first.")
    job["cwd"] = str(Path(job["cwd"]).expanduser().resolve())
    if not Path(job["cwd"]).is_dir():
        raise ValueError("The working directory does not exist.")
    anchor = datetime.fromisoformat(job["anchor"])
    job["next_at"] = wall_time(anchor.date(), anchor.time(), job["timezone"])
    if job["repeat"] == "weekdays" and anchor.weekday() >= 5:
        job["next_at"] = next_occurrence(job, job["next_at"] - 1)
    if job["next_at"] <= now:
        raise ValueError("Choose a future date and time.")
    job.update(id=job_id or uuid.uuid4().hex, enabled=True, running=False, last=None)
    def change(jobs):
        if job_id:
            old = next((j for j in jobs if j["id"] == job_id), None)
            if old is None:
                raise ValueError("This schedule was removed in another window.")
            if old.get("running"):
                raise ValueError("Wait for this run to finish before editing it.")
            job.update(last=old.get("last"), enabled=old["enabled"])
            jobs[jobs.index(old)] = job
        else:
            jobs.append(job)
        return deepcopy(job)
    return mutate(change)


def manage(job_id, action):
    def change(jobs):
        job = next((j for j in jobs if j["id"] == job_id), None)
        if job is None:
            raise ValueError("This schedule was removed in another window.")
        if action == "delete":
            if job.get("running"):
                raise ValueError("Pause now; remove after the current run finishes.")
            jobs.remove(job)
        elif action == "toggle":
            if not job["enabled"] and job["next_at"] is None:
                raise ValueError("Edit this completed schedule to choose a new time.")
            job["enabled"] = not job["enabled"]
        else:
            raise ValueError("Unknown schedule action.")
    mutate(change)


def profiles():
    by_name = {a["name"]: a for a in load_agents()}
    result = {name: by_name[name] for name in PROVIDERS[:2]}
    # This picker uses Gemini through the configured Antigravity CLI. If it
    # is missing, a separately installed native Gemini CLI can still be used.
    gemini = deepcopy(by_name["Antigravity"])
    if resolve_argv(gemini) is None:
        native = {"argv": ["gemini"]}
        if resolve_argv(native):
            gemini["argv"], gemini["executables"] = ["gemini"], []
            gemini["native_gemini"] = True
    result["Gemini"] = gemini
    return result


def command(job):
    agent = profiles()[job["provider"]]
    argv = resolve_argv(agent)
    if not argv:
        raise ValueError(f"{job['provider']} is missing. Configure it with agent --picker-doctor.")
    if job["provider"] == "Codex":
        # exec accepts the profile's model, effort, sandbox and config flags.
        # It cannot ask an unattended user for approval, and never adds bypass.
        return [argv[0], "exec", *argv[1:], "--skip-git-repo-check", "--", job["prompt"]]
    if job["provider"] == "Gemini" and not agent.get("native_gemini"):
        import agy_prompts
        argv = agy_prompts.prepare(agent, argv)
    return [*argv, "-p", job["prompt"]]


def recover():
    """Lost workers are uncertain, never automatically replay their effects."""
    def change(jobs):
        for job in jobs:
            if not job.get("running"):
                continue
            with lock(f"run-{job['id']}.lock", blocking=False) as held:
                if held is not None:
                    job["running"], job["enabled"] = False, False
                    job["last"].update(status="interrupted", finished=time.time(),
                                       error="Worker stopped. Inspect its log before rescheduling.")
    if any(j.get("running") for j in read()["jobs"]):
        mutate(change)


def run_job(job_id):
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise ValueError("Invalid schedule ID.")
    with lock(f"run-{job_id}.lock", blocking=False) as held:
        if held is None:
            return 0
        now = time.time()
        def claim(jobs):
            job = next((j for j in jobs if j["id"] == job_id), None)
            if not job or not job["enabled"] or job.get("running") or job["next_at"] is None or job["next_at"] > now:
                return None
            run_id = uuid.uuid4().hex
            log = directory() / f"{job_id}-{run_id}.log"
            job.update(running=True, next_at=next_occurrence(job, now))
            job["last"] = {"id": run_id, "status": "running", "started": now, "log": str(log)}
            return deepcopy(job)
        job = mutate(claim)
        if job is None:
            return 0
        result, error = 1, None
        process = None
        old_handlers = {}
        def stop(signum, frame):
            if process is not None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=5)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
            raise KeyboardInterrupt
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            old_handlers[sig] = signal.signal(sig, stop)
        try:
            log = Path(job["last"]["log"])
            fd = os.open(str(log), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                output.write(f"{job['provider']} · {job['cwd']} · {datetime.now().isoformat()}\n\n")
                output.flush()
                env = dict(os.environ)
                for key in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CODEX_THREAD_ID", "CODEX_INTERNAL_ORIGINATOR_OVERRIDE"):
                    env.pop(key, None)
                argv = command(job)
                if not Path(job["cwd"]).is_dir():
                    raise ValueError("The working directory no longer exists.")
                process = subprocess.Popen(argv, cwd=job["cwd"], env=env, stdin=subprocess.DEVNULL,
                                           stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
                result = process.wait()
                if result:
                    error = f"CLI exited with status {result}. See run log."
        except KeyboardInterrupt:
            error = "Run interrupted. Inspect its log before rescheduling."
            result = 130
        except Exception as exc:
            error = str(exc)
        finally:
            for sig, handler in old_handlers.items():
                signal.signal(sig, handler)
            def finish(jobs):
                row = next((j for j in jobs if j["id"] == job_id), None)
                if row and (row.get("last") or {}).get("id") == job["last"]["id"]:
                    row["running"] = False
                    row["last"].update(status="success" if result == 0 else "interrupted" if result == 130 else "failed",
                                       finished=time.time(), exit_code=result, error=error)
                    if result == 130:
                        row["enabled"] = False
            mutate(finish)
        return result


def due_ids(now=None):
    now = time.time() if now is None else now
    return [j["id"] for j in sorted(read()["jobs"], key=lambda j: j["next_at"] or float("inf"))
            if j["enabled"] and not j.get("running") and j["next_at"] is not None and j["next_at"] <= now]


def daemon():
    """No UI, network reads or quota polling. Sleep between tiny local checks."""
    with lock("daemon.lock", blocking=False) as held:
        if held is None:
            return 0
        workers = {}
        stopped = threading.Event()
        def stop(signum, frame):
            stopped.set()
        old = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
        try:
            while not stopped.is_set():
                workers = {key: p for key, p in workers.items() if p.poll() is None}
                try:
                    recover()
                    for job_id in due_ids():
                        if len(workers) >= MAX_WORKERS:
                            break
                        if job_id in workers:
                            continue
                        workers[job_id] = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--run", job_id],
                                                           stdin=subprocess.DEVNULL, close_fds=True)
                except Exception as exc:
                    print(f"scheduler: {exc}", file=sys.stderr, flush=True)
                # No timer requests a system wake. Resume catches overdue jobs.
                stopped.wait(POLL_SECONDS)
        finally:
            for sig, handler in old.items():
                signal.signal(sig, handler)
            for process in workers.values():
                if process.poll() is None:
                    process.terminate()
            for process in workers.values():
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        return 0


def plist_path():
    return Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"


def service_status():
    result = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"],
                            capture_output=True, text=True)
    return result.returncode == 0


def install_service():
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    base = directory()
    # Create the private file on first setup, preserving any existing entries.
    mutate(lambda jobs: None)
    path_env = os.pathsep.join(dict.fromkeys([*os.environ.get("PATH", "").split(os.pathsep),
                                            *(str(Path(d).expanduser()) for d in SEARCH_DIRS), "/usr/bin", "/bin"]))
    env = {"PATH": path_env}
    for key in ("AGENT_PICKER_CONFIG", "AGENT_PICKER_STATE_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME"):
        if key in os.environ:
            env[key] = os.environ[key]
    content = plistlib.dumps({"Label": LABEL, "ProgramArguments": [sys.executable, str(Path(__file__).resolve()), "--daemon"],
                             "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 30,
                             "ProcessType": "Background", "LowPriorityIO": True, "Nice": 10,
                             "EnvironmentVariables": env, "WorkingDirectory": str(Path.home()),
                             "StandardOutPath": str(base / "service.log"), "StandardErrorPath": str(base / "service.log")})
    target = f"gui/{os.getuid()}/{LABEL}"
    loaded = service_status()
    if loaded and path.exists() and path.read_bytes() == content:
        return path
    # Refresh only while no runs are in progress; unloading terminates children.
    if loaded and any(j.get("running") for j in read()["jobs"]):
        raise ValueError("Scheduler is running prompts. Wait before reinstalling its service.")
    if loaded:
        subprocess.run(["launchctl", "bootout", target], check=True, capture_output=True)
    path.write_bytes(content)
    path.chmod(0o600)
    for name in ("service.log",):
        fd = os.open(str(base / name), os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        os.close(fd)
    result = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"Could not start scheduler: {result.stderr.strip()}")
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local agent prompt scheduler. Open without arguments for the menu.")
    parser.add_argument("--daemon", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--run", help=argparse.SUPPRESS)
    parser.add_argument("--install-service", action="store_true", help="enable the macOS background dispatcher")
    parser.add_argument("--status", action="store_true", help="show service and private schedule paths")
    args = parser.parse_args(argv)
    if args.daemon:
        return daemon()
    if args.run:
        return run_job(args.run)
    if args.install_service:
        print(f"Scheduler enabled: {install_service()}")
        return 0
    if args.status:
        print(f"Service: {'loaded' if service_status() else 'not loaded'}\nSchedules: {schedule_path()}\nLogs: {directory()}")
        return 0
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        parser.error("the scheduler menu needs an interactive terminal")
    import agent_picker
    try:
        index = agent_picker.choose("scheduler")
    except KeyboardInterrupt:
        return 0
    if index is not None:
        # Returning to the selector retains its normal launch path, including
        # provider-specific preparation and the user's local defaults.
        os.execv(sys.executable, [sys.executable, str(Path(__file__).with_name("agent_picker.py")),
                                "--picker-launch", agent_picker.AGENTS[index]["name"]])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError) as exc:
        sys.exit(f"schedule: {exc}")
