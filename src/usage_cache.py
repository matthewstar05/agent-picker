"""Freshest usage readings shared by picker, panels, and concurrent Star panes."""

import json
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

_THREAD_LOCK = threading.Lock()


def read(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return {name: entry for name, entry in data.items() if isinstance(entry, dict)}
    except (OSError, ValueError, AttributeError):
        return {}


def _stamp(entry):
    try:
        return float(entry.get("fetched", 0) or 0)
    except (TypeError, ValueError, AttributeError):
        return 0


def merge(current, incoming):
    out = dict(current)
    for name, entry in incoming.items():
        if isinstance(entry, dict) and (name not in out or _stamp(entry) > _stamp(out[name])):
            out[name] = entry
    return out


@contextmanager
def _locked(path):
    # The process lock also releases after a crash. The thread lock covers
    # callers sharing a process.
    with _THREAD_LOCK, path.open("a+b") as handle:
        import fcntl
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def write(path, readings):
    """Merge under a cross-process lock, atomically save, return all fresh readings."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _locked(path.with_name(path.name + ".lock")):
        disk = read(path)
        combined = merge(disk, readings)
        if combined == disk and path.exists():
            return combined
        fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(combined, handle)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return combined
