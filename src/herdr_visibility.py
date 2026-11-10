"""Selected-tab visibility for terminal UIs, driven by Herdr's socket events.

Only focus/topology changes request a fresh snapshot. An idle connection blocks
in read(), so hidden screens do not poll Herdr or spawn CLI processes per frame.
Outside Herdr, or when its API is unavailable, normal terminal behavior remains.
"""

import json
import os
import socket
import threading

from herdr_sidebar import request


EVENTS = ("tab.focused", "workspace.focused", "tab.closed", "workspace.closed", "pane.moved")
MAX_LINE = 4 * 1024 * 1024


class Visibility:
    def __init__(self):
        self.path = os.environ.get("HERDR_SOCKET_PATH") if os.environ.get("HERDR_ENV") == "1" else None
        self.pane = os.environ.get("HERDR_PANE_ID")
        self.tab = os.environ.get("HERDR_TAB_ID")
        self.workspace = os.environ.get("HERDR_WORKSPACE_ID")
        self.terminal = None
        self.enabled = bool(self.path and (self.pane or self.tab) and hasattr(socket, "AF_UNIX"))
        self.visible = True
        self.known = False
        self._stop = threading.Event()
        self._socket = None
        self._thread = None

    def start(self):
        if self.enabled and self._thread is None:
            self.visible = False  # wait for the first snapshot before drawing
            self._thread = threading.Thread(target=self._watch, name="herdr-visibility", daemon=True)
            self._thread.start()
        return self

    def snapshot(self, snapshot):
        if not isinstance(snapshot, dict):
            raise ValueError("Invalid Herdr visibility snapshot")
        panes = snapshot.get("panes", [])
        pane = next((p for p in panes if isinstance(p, dict) and (p.get("terminal_id") == self.terminal if self.terminal
                                        else p.get("pane_id") == self.pane)), None)
        if pane:
            self.terminal = pane.get("terminal_id")
            self.pane, self.tab, self.workspace = pane["pane_id"], pane["tab_id"], pane["workspace_id"]
        self.visible = (snapshot.get("focused_tab_id") == self.tab and
                        snapshot.get("focused_workspace_id") == self.workspace)
        self.known = True

    def _refresh(self):
        self.snapshot(request("session.snapshot", {}, self.path)["snapshot"])

    def _watch(self):
        delay = 1.0
        while not self._stop.is_set():
            client = None
            try:
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self._socket = client
                client.settimeout(2)
                client.connect(self.path)
                client.sendall((json.dumps({"id": "visibility", "method": "events.subscribe", "params": {
                    "subscriptions": [{"type": event} for event in EVENTS]}}) + "\n").encode())
                with client.makefile("rb") as stream:
                    ack = json.loads(stream.readline(MAX_LINE))
                    if not isinstance(ack, dict) or ack.get("id") != "visibility" or "error" in ack:
                        raise OSError("Herdr visibility subscription unavailable")
                    # A subscribed socket is an event stream, not a request socket.
                    # Subscribe first, then snapshot on a separate short-lived connection.
                    self._refresh()
                    delay = 1.0
                    client.settimeout(None)
                    while not self._stop.is_set():
                        line = stream.readline(MAX_LINE)
                        if not line:
                            raise OSError("Herdr visibility stream closed")
                        event = json.loads(line)
                        kind = event.get("event") if isinstance(event, dict) else None
                        if isinstance(kind, str) and kind.replace("_", ".", 1) in EVENTS:
                            self._refresh()
            except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                if not self.known:
                    self.visible = True   # an older/unavailable API must not leave a blank UI
                else:
                    self.visible = False  # stop stale animations until the stream reconnects
            finally:
                if client is not None:
                    client.close()
                self._socket = None
            if self._stop.wait(delay):
                break
            delay = min(30.0, delay * 2)

    def close(self):
        self._stop.set()
        client = self._socket
        if client is not None:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=3)
