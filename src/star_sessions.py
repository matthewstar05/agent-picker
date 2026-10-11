"""One writer for a Star conversation, released by the OS after a crash."""

import os
import re
from pathlib import Path


def valid_id(ident):
    return isinstance(ident, str) and bool(re.fullmatch(r"[A-Za-z0-9_-]+", ident))


def valid_conversation(data):
    if not isinstance(data, dict) or not valid_id(data.get("id")):
        return False
    for key in ("cwd", "goal", "goal_state", "summary"):
        if key in data and not isinstance(data[key], str):
            return False
    for key in ("requests", "notes", "mesh_replies"):
        if key in data and (not isinstance(data[key], list) or
                            any(not isinstance(item, str) for item in data[key])):
            return False
    turns = data.get("turns", [])
    if not isinstance(turns, list) or any(not isinstance(turn, dict) or any(
            not isinstance(turn.get(key), str) for key in ("user", "reply", "provider")) for turn in turns):
        return False
    for key, kind in (("sessions", str), ("briefed", str), ("seen", int), ("mesh_tasks", dict)):
        values = data.get(key, {})
        if not isinstance(values, dict) or any(not isinstance(k, str) or not isinstance(v, kind)
                                                for k, v in values.items()):
            return False
    inflight = data.get("inflight")
    if inflight is not None and (not isinstance(inflight, dict) or any(
            not isinstance(inflight.get(key), str) for key in ("provider", "prompt"))):
        return False
    return True

class ConversationBusy(RuntimeError):
    pass


class Lease:
    def __init__(self, folder, check_turn=True):
        self.folder, self.ident, self.handle = Path(folder), None, None
        self.check_turn = check_turn

    def __enter__(self):
        return self

    def claim(self, ident, prepare=None):
        if not valid_id(ident):
            raise ValueError("invalid Star conversation ID")
        if self.ident == ident:
            return prepare() if prepare else None
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / (ident + ".lock")
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
        handle = os.fdopen(fd, "r+b")
        try:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise ConversationBusy(f"conversation {ident} is already open in another Star process")
        if self.check_turn:
            try:
                with Lease(self.folder / "turns", check_turn=False) as turn:
                    turn.claim(ident)
            except ConversationBusy:
                handle.close()
                raise ConversationBusy(f"conversation {ident} still has a provider stopping; retry shortly")
        try:
            result = prepare() if prepare else None
        except BaseException:
            handle.close()
            raise
        # Claim and prepare first: a failed /resume keeps the original owned.
        self.close()
        self.ident, self.handle = ident, handle
        return result

    def close(self):
        if self.handle is not None:
            try:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_UN)
            finally:
                self.handle.close()
                self.handle, self.ident = None, None

    def __exit__(self, *exc):
        self.close()
