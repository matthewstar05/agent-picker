"""Private, crash-safe CLI conversation bindings; one writer per bot."""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import tempfile
import time
import uuid

from platform_runtime import state_dir
from grokbot_gateway import GrokError


def directory():
    path = state_dir() / "grokbot" / "sessions"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def valid_id(ident):
    try:
        return str(uuid.UUID(ident)) == ident
    except (ValueError, TypeError, AttributeError):
        return False


def save(session):
    if not valid_id(session.get("id")):
        raise GrokError("Invalid saved Grok Bot session identifier.")
    session["updated"] = time.time()
    path = directory() / (session["id"] + ".json")
    fd, temporary = tempfile.mkstemp(prefix=".session-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(session, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def sessions(cwd=None):
    rows = []
    for path in directory().glob("*.json"):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
            if (isinstance(row, dict) and valid_id(row.get("id")) and row.get("id") == path.stem
                    and isinstance(row.get("cwd"), str) and (cwd is None or row["cwd"] == str(cwd))):
                rows.append(row)
        except (OSError, ValueError):
            continue
    return sorted(rows, key=lambda row: row.get("updated", 0), reverse=True)


def load(ident=None, cwd=None):
    rows = sessions(cwd)
    matches = [row for row in rows if not ident or row["id"].startswith(ident)]
    if not matches:
        raise GrokError("No matching CLI session. Run grokbot ls, or start a new conversation.")
    if ident and len(matches) > 1:
        raise GrokError("Ambiguous session prefix. Use the full ID from grokbot ls.")
    return matches[0]


@contextmanager
def lease(session):
    if not valid_id(session.get("id")):
        raise GrokError("Invalid session identifier.")
    path = directory() / (session["id"] + ".lock")
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise GrokError("This conversation is open in another CLI. Start a new conversation or close that CLI.") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
