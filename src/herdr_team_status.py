"""Display team activity without taking over any provider's lifecycle hooks."""

import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import herdr_sidebar as sidebar
import preferences
from platform_runtime import WINDOWS, state_dir

STATUSES = ("blocked", "crew_working", "working", "done", "idle", "unknown")
RANK = {"blocked": 4, "done": 3, "working": 2, "idle": 1, "unknown": 0}
TOKEN_PREFIX = "herdr_ai_icon_"


def effective_statuses(panes, seen=None):
    busy_teams = {(p.get("workspace_id"), p.get("tab_id")) for p in panes
                  if (p.get("tokens") or {}).get(sidebar.ROLE) == "crew"
                  and p.get("agent") and p.get("agent_status") == "working"}
    result = {}
    for pane in panes:
        if not pane.get("agent"):
            continue
        status = pane.get("agent_status", "unknown")
        if status not in RANK:
            status = "unknown"
        if seen is not None and pane.get("completion_seq"):
            terminal = pane.get("terminal_id", pane["pane_id"])
            completion = pane["completion_seq"]
            if pane.get("focused"):
                seen[terminal] = completion
            if status == "idle" and completion > seen.get(terminal, 0):
                status = "done"
        if ((pane.get("tokens") or {}).get(sidebar.ROLE) == "lead"
                and (pane.get("workspace_id"), pane.get("tab_id")) in busy_teams
                and status != "blocked"):
            status = "crew_working"
        result[pane["pane_id"]] = status
    return result


def publish(panes, refreshed, call=sidebar.request, now=None, symbols=False, seen=None):
    now = time.monotonic() if now is None else now
    effective = effective_statuses(panes, seen)
    for pane in panes:
        pid = pane["pane_id"]
        if pid not in effective:
            continue
        status = effective[pid]
        identity = (pane.get("terminal_id"), status, symbols)
        previous = refreshed.get(pid)
        if previous and previous[0] == identity and now - previous[1] < 4:
            continue
        native_status = "working" if status == "crew_working" else status
        glyph = {"idle": "○", "unknown": "·"}.get(status, "●")
        if symbols:
            glyph = {"blocked": "×", "working": "◐", "crew_working": "◐", "done": "✓"}.get(status, glyph)
        tokens = {TOKEN_PREFIX + key: glyph if key == status else None for key in STATUSES}
        tokens.update({"herdr_ai_priority": str(RANK[native_status]), "herdr_ai_status": native_status})
        call("pane.report_metadata", {
            "pane_id": pid, "source": "custom:herdr-ai-status", "agent": pane["agent"],
            "tokens": tokens, "ttl_ms": 10000})
        refreshed[pid] = (identity, now)
    for pid in set(refreshed) - set(effective):
        refreshed.pop(pid)
    return effective


def config_path():
    if os.environ.get("HERDR_CONFIG_PATH"):
        return Path(os.environ["HERDR_CONFIG_PATH"]).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME")
    if not base:
        base = os.environ.get("APPDATA") if WINDOWS else None
    return Path(base or str(Path.home() / ".config")) / "herdr/config.toml"


def section(text, name):
    match = re.search(r"(?m)^\[" + re.escape(name) + r"\]\s*\n(.*?)(?=^\[|\Z)", text, re.S)
    return match[1] if match else ""


def colors(text):
    # Native palettes from Herdr 0.9.3 (Apache-2.0), src/app/state.rs.
    palettes = json.loads(Path(__file__).with_name("herdr_status_palettes.json").read_text(encoding="utf-8"))
    name = re.search(r'(?m)^\s*name\s*=\s*["\']([^"\']+)', section(text, "theme"))
    palette = dict(palettes.get(name[1] if name else "catppuccin", palettes["catppuccin"]))
    overrides = section(text, "theme.custom")
    for status, key in {"working": "yellow", "blocked": "red", "done": "teal",
                        "idle": "green", "unknown": "overlay0", "crew_working": "peach"}.items():
        match = re.search(r'(?m)^\s*' + key + r'\s*=\s*["\'](#[0-9a-fA-F]{6})["\']', overrides)
        if match:
            palette[status] = match[1]
    return palette


def indicator_config(text):
    palette = colors(text)
    icons = ", ".join('{ token = "$' + TOKEN_PREFIX + s + '", fg = "' + palette[s] + '" }'
                      for s in STATUSES)
    parts = re.split(r"(?m)(^\[[^\n]+\]\s*\n)", text)
    found = False
    for i in range(1, len(parts), 2):
        header = parts[i].strip()
        if header not in ("[ui.sidebar.agents]", "[ui.sidebar.agents.rows_by_agent]"):
            continue
        found = found or header == "[ui.sidebar.agents]"
        # The native layout uses plain state_icon tokens. Preserve all other
        # rows, task labels, sorting, keys, and theme settings byte for byte.
        parts[i + 1] = re.sub(r'\{[^{}]*\btoken\s*=\s*(["\'])state_icon\1[^{}]*\}',
                              lambda _: icons, parts[i + 1])
        parts[i + 1] = re.sub(r'(?<![\w$])(["\'])state_icon\1', lambda _: icons, parts[i + 1])
        for status in STATUSES:
            pattern = r'(token\s*=\s*"\$' + TOKEN_PREFIX + status + r'"\s*,\s*fg\s*=\s*")[^"]+("\s*\})'
            parts[i + 1] = re.sub(pattern, lambda m, s=status: m[1] + palette[s] + m[2], parts[i + 1])
    result = "".join(parts)
    if not found:
        result += '\n[ui.sidebar.agents]\nrows = [[' + icons + ', "machine", "workspace", "tab"], ["agent"]]\n'
    return result


def install_indicators(call=sidebar.request):
    path = config_path()
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    updated = indicator_config(original)
    if updated != original:
        backup = state_dir() / "herdr-ai-sidebar-config-original.toml"
        if not backup.exists():
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_text(original, encoding="utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(updated, encoding="utf-8")
        try:
            result = call("server.reload_config", {})
            if result.get("status") in ("failed", "partial"):
                raise RuntimeError("Herdr rejected the team status layout: " + str(result.get("diagnostics", [])))
        except Exception:
            path.write_text(original, encoding="utf-8")
            call("server.reload_config", {})
            raise
    return updated


@contextlib.contextmanager
def watcher_lock(socket_path):
    key = hashlib.sha256(socket_path.encode("utf-8")).hexdigest()[:16]
    path = state_dir() / ("herdr-ai-status-" + key + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if WINDOWS:
            import msvcrt
            if path.stat().st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                yield False
                return
            try:
                yield True
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def ensure_running():
    path = sidebar.endpoint()
    with watcher_lock(path) as acquired:
        if not acquired:
            return
    options = ({"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
               if WINDOWS else {"start_new_session": True})
    log_path = state_dir() / "herdr-ai-status.log"
    with log_path.open("ab") as log:
        subprocess.Popen([sys.executable, str(Path(__file__).with_name("herdr_ai.py")), "--watch-sidebar"],
                         env={**os.environ, "HERDR_SOCKET_PATH": path}, stdin=subprocess.DEVNULL,
                         stdout=log, stderr=log, close_fds=True, **options)


def run():
    path = sidebar.endpoint()
    call = lambda method, params: sidebar.request(method, params, path)
    with watcher_lock(path) as acquired:
        if not acquired:
            return
        refreshed, previous_settings = {}, None
        seen_path = state_dir() / ("herdr-ai-seen-" + hashlib.sha256(path.encode()).hexdigest()[:16] + ".json")
        try:
            seen = json.loads(seen_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            seen = {}
        while True:
            try:
                panes = call("session.snapshot", {}).get("snapshot", {}).get("agents", [])
                text = config_path().read_text(encoding="utf-8") if config_path().exists() else ""
                symbols = bool(re.search(r'(?m)^\s*status_indicators\s*=\s*"symbols"', section(text, "ui")))
                before_seen = dict(seen)
                publish(panes, refreshed, call, symbols=symbols, seen=seen)
                if seen != before_seen:
                    seen_path.write_text(json.dumps(seen) + "\n", encoding="utf-8")
                install_indicators(call)
                preferences._values = None
                for setting in sidebar.SETTINGS:
                    preferences.register(setting)
                selected = tuple(preferences.get(s.key) for s in sidebar.SETTINGS)
                if selected != previous_settings:
                    sidebar.sync(socket_path=path, call=call, migrate=False)
                    previous_settings = selected
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                print(f"herdr-ai status: {exc}", file=sys.stderr, flush=True)
                refreshed.clear()
            time.sleep(1)
