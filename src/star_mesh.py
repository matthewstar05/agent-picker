"""The Star mesh: Star CLIs that know about each other and hand each other work.

Every StarCLI registers itself under STATE/star/mesh/members: its name, role
(lead, crew or solo), pid, herdr pane, tab and workspace, working folder, and
what it is doing (idle or working, the task, the provider it is driving, its
goal). Peers are the other live members in the same herdr tab, or on this
computer when StarCLI runs outside herdr.

A message to a peer lands in its inbox (mesh/inbox/<name>/). An idle StarCLI
picks a task up, runs it as a turn (routed and balanced like any other), and
answers: the reply goes to mesh/replies/<id>.json for a sender that waits on it
(`star --send NAME TASK --wait`, which is how the agents inside StarCLI
delegate), and into the sender's inbox when the sender is another StarCLI, so
it shows up there and reaches its next turn.

Files are written atomically and read leniently: a member whose process is
gone is ignored and its files cleaned up.
"""

import json
import os
import re
import shlex
import sys
import time
import uuid
import weakref
from pathlib import Path

import agent_picker as ap
from star_sessions import ConversationBusy, Lease

ROOT = ap.STATE / "star" / "mesh"
MEMBERS = ROOT / "members"
INBOX = ROOT / "inbox"
REPLIES = ROOT / "replies"
STARCLI = Path(__file__).resolve().parent / "starcli.py"


def command():
    """How an agent's shell runs StarCLI: this interpreter and this checkout, no alias needed."""
    if ap.WINDOWS:
        # Native Windows providers use PowerShell. Quoted executable paths
        # require its call operator, and apostrophes escape by doubling.
        return "& " + " ".join("'" + str(w).replace("'", "''") + "'" for w in (sys.executable, STARCLI))
    return shlex.join([sys.executable, str(STARCLI)])


def parallel_note():
    if ap.WINDOWS:
        return ("Use PowerShell for these commands. Independent sends can run in Start-Job script blocks; "
                "use Wait-Job and Receive-Job to collect their reports.")
    return "Several can run at once in the background, each redirected to its own file, then `wait`."


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def _read(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if ap.WINDOWS:
        # os.kill(pid, 0) is not a safe liveness probe on Windows. Wait on a
        # process handle without signalling it; permission failures stay unknown.
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only
        if not handle:
            return ctypes.get_last_error() != 87  # ERROR_INVALID_PARAMETER: no such pid
        try:
            return kernel.WaitForSingleObject(handle, 0) != 0  # signalled means exited
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, ValueError, TypeError):
        return True
    return True


def herdr_place():
    return {"pane": os.environ.get("HERDR_PANE_ID"), "tab": os.environ.get("HERDR_TAB_ID"),
            "workspace": os.environ.get("HERDR_WORKSPACE_ID"),
            "socket": os.environ.get("HERDR_SOCKET_PATH") or os.environ.get("STAR_MESH_SOCKET")}


def members():
    """Every live member, oldest first; stale files are removed."""
    found = []
    for path in sorted(MEMBERS.glob("*.json")) if MEMBERS.is_dir() else ():
        data = _read(path)
        if not data:
            continue
        if not alive(data.get("pid")):
            try:
                path.unlink()
            except OSError:
                pass
            continue
        found.append(data)
    return sorted(found, key=lambda m: m.get("joined", 0))


def place_key(place):
    """Names are unique per herdr tab (or per computer outside herdr)."""
    tab = safe(place.get("tab") or "local")
    if place.get("socket"):
        import hashlib
        tab += "-" + hashlib.sha256(place["socket"].encode("utf-8")).hexdigest()[:12]
    return tab


def same_place(a, b):
    if a.get("tab") or b.get("tab"):
        return (a.get("tab") == b.get("tab") and a.get("workspace") == b.get("workspace")
                and a.get("socket") == b.get("socket"))
    return True


def safe(name):
    return re.sub(r"[^A-Za-z0-9_.-]", "-", name)[:40] or "star"


class Member:
    """This StarCLI's own entry in the mesh."""

    def __init__(self, name=None, role="solo", cwd=None):
        self.place = herdr_place()
        self.role = role
        self.lease = Lease(ROOT / "names" / place_key(self.place), check_turn=False)
        if name:
            self.name = safe(name)
            try:
                self.lease.claim(self.name)
            except ConversationBusy:
                raise ConversationBusy(f"Star name {self.name!r} is already running in this mesh")
        else:
            self.name = self._free_name()
        self.release_name = weakref.finalize(self, self.lease.close)
        self.state = {"name": self.name, "role": role, "pid": os.getpid(), "cwd": cwd or os.getcwd(),
                      "joined": time.time(), "status": "idle", "task": "", "provider": "", "goal": "",
                      "ready": False, "draft": False,
                      **self.place}
        self.path = MEMBERS / f"{self.name}-{os.getpid()}.json"
        self.box = INBOX / place_key(self.place) / self.name
        self.beat()

    def _free_name(self):
        taken = {m["name"] for m in members() if same_place(m, self.place)}
        n = 1
        while True:
            candidate = f"star{n}"
            if candidate not in taken:
                try:
                    self.lease.claim(candidate)
                    return candidate
                except ConversationBusy:
                    pass
            n += 1

    def beat(self, **changes):
        self.state.update(changes, updated=time.time())
        try:
            _write(self.path, self.state)
        except OSError:
            pass

    def leave(self):
        for path in (self.path,):
            try:
                path.unlink()
            except OSError:
                pass
        self.release_name()

    def peers(self):
        return [m for m in members() if m["name"] != self.name and same_place(m, self.state)]

    def inbox(self):
        """Messages waiting for this member, oldest first."""
        folder = self.box
        out = []
        for path in sorted(folder.glob("*.json")) if folder.is_dir() else ():
            data = _read(path)
            if data:
                data["_path"] = str(path)
                out.append(data)
        return out

    def take(self, message):
        try:
            os.unlink(message["_path"])
        except (OSError, KeyError):
            pass

    def outstanding(self):
        """Tasks this member sent that their recipients have not acknowledged yet.

        Recipients keep task files until a durable answer is delivered, including
        while running and across refreshes. This also covers senders using --wait.
        """
        folder = INBOX / place_key(self.place)
        tasks = []
        for path in folder.glob("*/*.json") if folder.is_dir() else ():
            message = _read(path)
            if message and message.get("kind") == "task" and message.get("from") == self.name:
                tasks.append((path.parent.name, message))
        return tasks

    def brief(self):
        """What the agents inside this StarCLI are told about the mesh, or "" when alone.

        A lead or crew always hears the usage rules, even before its peers have registered.
        A solo StarCLI with no peers has nothing to add."""
        peers = self.peers()
        if not peers and self.role not in ("lead", "crew"):
            return ""
        lines = [usage_note(self.role)]
        if not peers:
            return lines[0]
        star = command()
        lines += [f"[StarCLI mesh] You are running inside StarCLI as '{self.name}' (role: {self.role}) in a "
                  f"herdr tab with other StarCLI terminals. Each one routes its tasks to the best AI provider "
                  f"with quota, so work you hand them spreads the load across providers.",
                  "Peers right now:"]
        for m in peers:
            doing = f"working on: {m['task'][:80]}" if m.get("status") == "working" and m.get("task") else m.get("status", "idle")
            lines.append(f"- {m['name']} ({m.get('role', 'solo')}): {doing}")
        lines.append(f"To hand a peer a self-contained task and wait for its report, run in your shell: "
                     f"{star} --send <name> \"<task>\" --wait   (prints the peer's reply). {parallel_note()} "
                     f"{star} --peers shows who is free.")
        if self.role == "lead":
            lines.append("As the lead, keep planning, decisions and integration for yourself and delegate "
                         "searches, test runs, well specified edits, research and reviews to idle crew, in "
                         "parallel where tasks are independent. Give each task the files, the goal and what "
                         "done looks like; check what comes back.")
        elif self.role == "crew":
            lines.append("As crew, do the tasks you are given and finish with a short report (at most 15 lines): "
                         "what you did, files changed, open questions.")
        return "\n".join(lines)


def usage_note(role):
    """The herdr-ai usage rules for a pane that stays put and routes its own turns."""
    import handoff
    cmd, near = handoff.command(), handoff.NEAR
    check = (f"Usage limits: run `{cmd} --usage` before a long task and after each major step. "
             f"StarCLI routes each turn to a provider below {near}%. Do not run `{cmd} --handoff`; "
             f"this pane stays.")
    if role == "crew":
        return (check + f" If the provider you are on is at {near}% or more of any limit, stop at a safe "
                f"point and say so at the top of your reply; the lead decides who continues.")
    return (check + f" When every provider is at {near}% or more and the goal is unfinished, tell the user "
            f"and stop. Do not hand off a finished goal.")


def find(name, place=None):
    """The live member called name in this tab (or on this computer outside herdr)."""
    place = place or herdr_place()
    return next((m for m in members() if m["name"] == name and same_place(m, place)), None)


def send(to, text, sender="you", wait=False, timeout=1800, sender_is_star=False, poll=0.5):
    """Put a task in a peer's inbox. With wait, block until it answers and return the
    reply text (None on timeout). Returns the message id otherwise."""
    target = find(to)
    if not target:
        raise LookupError(f"no StarCLI named {to!r} is running; star --peers lists them")
    mid = f"{time.time():.6f}-{uuid.uuid4().hex[:8]}"
    _write(INBOX / place_key(target) / target["name"] / f"{mid}.json",
           {"id": mid, "kind": "task", "from": sender, "text": text, "sent": time.time(),
            "reply_to_star": sender if sender_is_star else None})
    if not wait:
        return mid
    deadline = time.time() + timeout
    path = REPLIES / f"{mid}.json"
    while time.time() < deadline:
        data = _read(path)
        if data:
            try:
                path.unlink()
            except OSError:
                pass
            return data.get("text", "")
        if not alive(target.get("pid")):
            raise LookupError(f"{to} stopped before answering")
        time.sleep(poll)
    return None


def reply(message, text, sender):
    """Answer a task: for a waiting sender, and in a sending StarCLI's inbox."""
    _write(REPLIES / f"{message['id']}.json", {"id": message["id"], "from": sender, "text": text,
                                               "answered": time.time()})
    star = message.get("reply_to_star")
    if star and find(star):
        _write(INBOX / place_key(herdr_place()) / star / f"{time.time():.6f}-{uuid.uuid4().hex[:8]}.json",
               {"id": message["id"], "kind": "reply", "from": sender, "text": text, "task": message.get("text", ""),
                "sent": time.time()})
    # an unclaimed reply nobody waits for is cleaned up after a day
    cutoff = time.time() - 86400
    for path in REPLIES.glob("*.json") if REPLIES.is_dir() else ():
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def describe(me=None):
    """Lines for `star --peers` and /peers."""
    everyone = members()
    here = herdr_place()
    shown = [m for m in everyone if same_place(m, here)] if here.get("tab") else everyone
    if not shown:
        return ["no StarCLI is running" + (" in this tab" if here.get("tab") else "")]
    out = []
    for m in shown:
        mark = "*" if me and m["name"] == me else " "
        doing = m.get("status", "idle")
        if m.get("status") == "working":
            doing = f"working with {m.get('provider') or '…'}: {m.get('task', '')[:60]}"
        if m.get("goal"):
            doing += f"  [goal: {m['goal'][:40]}]"
        out.append(f"{mark} {m['name']:<10} {m.get('role', 'solo'):<5} {doing}")
    return out
