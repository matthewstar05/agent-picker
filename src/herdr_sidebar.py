"""Crew visibility in Herdr's Agents view, without changing agent lifecycle."""

import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import preferences
from platform_runtime import state_dir

SOURCE = "custom:herdr-ai"
ROLE = "herdr_ai_role"
HIDE_CREW = preferences.Setting(
    "herdr_ai.hide_crew", "Herdr AI", "Hide crew agents", [False, True], True,
    hint="Hide crew from Herdr's Agents sidebar; keep the lead visible.",
    names={False: "off", True: "on"})
ORDER = preferences.Setting(
    "herdr_ai.agent_order", "Herdr AI", "Agent order", ["default", "priority"], "priority",
    hint="Group agents by workspace, or put active and finished agents first.",
    names={"default": "grouped", "priority": "priority"})
SETTINGS = (HIDE_CREW, ORDER)

def endpoint():
    if os.environ.get("HERDR_SOCKET_PATH"):
        return os.environ["HERDR_SOCKET_PATH"]
    binary = os.environ.get("HERDR_BIN_PATH", "herdr")
    out = subprocess.run([binary, "status"], capture_output=True, text=True, timeout=3)
    match = re.search(r"^\s*socket:\s*(.+)$", out.stdout, re.M)
    if out.returncode or not match:
        raise RuntimeError("Herdr is not running; saved visibility applies next time herdr-ai runs")
    return match[1].strip()


def request(method, params, socket_path=None):
    path = socket_path or endpoint()
    rid = f"herdr-ai:{time.time_ns()}"
    payload = json.dumps({"id": rid, "method": method, "params": params})
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2)
        client.connect(path)
        client.sendall((payload + "\n").encode("utf-8"))
        with client.makefile("rb") as stream:
            for _ in range(32):
                line = stream.readline(4 * 1024 * 1024)
                if not line:
                    raise RuntimeError("Herdr closed the connection")
                response = json.loads(line)
                if response.get("id") == rid:
                    break
            else:
                raise RuntimeError("No matching Herdr response")
    if "error" in response:
        error = response["error"]
        raise RuntimeError(error.get("message", str(error)))
    return response["result"]


def mark_roles(panes, call=request):
    for role, pane in panes.items():
        if role == "lead" or role.startswith("crew"):
            call("pane.report_metadata", {"pane_id": pane, "source": SOURCE,
                                         "tokens": {ROLE: "lead" if role == "lead" else "crew"}})


def restore_roles(state, path, live, call):
    """Migrate saved layouts, checking terminal identity so reused panes stay visible."""
    for key, entry in state.items():
        if not key.startswith(path + "|") or not entry.get("team") or entry.get("mode") == "individual":
            continue   # individual peers have no lead or crew; all stay visible
        panes = {r: p for r, p in entry.get("panes", {}).items()
                 if p in live and live[p].get("terminal_id") == entry.get("terminals", {}).get(p)}
        mark_roles(panes, call)


def legacy_roles(live, call):
    """Older workspaces created outside Herdr were not recorded. Recover only
    exact team briefs in live process arguments, never names or task titles."""
    pattern = re.compile(r"wait in the herdr panes ([^\n]+?) on this tab, in your working directory\.")
    for pane, info in live.items():
        if not info.get("agent") or (info.get("tokens") or {}).get(ROLE):
            continue
        process = call("pane.process_info", {"pane_id": pane}).get("process_info", {})
        for proc in process.get("foreground_processes", []):
            for arg in proc.get("argv", []):
                if "You lead a herdr team. Four " not in arg:
                    continue
                match = pattern.search(arg)
                crew = match[1].split(", ") if match else []
                if len(crew) == 4 and len(set(crew)) == 4 and pane not in crew and all(
                        p in live and live[p].get("tab_id") == info.get("tab_id") for p in crew):
                    mark_roles({"lead": pane, **dict(zip(("crew1", "crew2", "crew3", "crew4"), crew))}, call)


def sync(state=None, socket_path=None, call=None, migrate=True, restore=False):
    for setting in SETTINGS:
        preferences.register(setting)
    path = socket_path or endpoint()
    call = call or (lambda method, params: request(method, params, path))
    if migrate:
        live = {p["pane_id"]: p for p in call("pane.list", {}).get("panes", [])}
        registry = state_dir() / "herdr-ai-roles.json"
        try:
            saved = json.loads(registry.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        records = saved.get(path, {})
        for pane, info in live.items():
            record = records.get(info.get("terminal_id"))
            if restore and record is None:
                # Herdr preserves public pane/tab IDs during restore but allocates
                # new terminal IDs. Permit that fallback only in the startup hook.
                record = next((r for r in records.values() if isinstance(r, dict)
                               and r.get("pane_id") == pane and r.get("tab_id") == info.get("tab_id")), None)
            role = record.get("role") if isinstance(record, dict) else record
            if role in ("lead", "crew") and not (info.get("tokens") or {}).get(ROLE):
                mark_roles({role: pane}, call)
        restore_roles(state or {}, path, live, call)
        legacy_roles(live, call)
        refreshed = call("pane.list", {}).get("panes", [])
        saved[path] = {p["terminal_id"]: {"role": p["tokens"][ROLE], "pane_id": p["pane_id"],
                                         "tab_id": p.get("tab_id")} for p in refreshed
                       if p.get("terminal_id") and (p.get("tokens") or {}).get(ROLE) in ("lead", "crew")}
        registry.parent.mkdir(parents=True, exist_ok=True)
        temporary = registry.with_name(f".herdr-ai-roles.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(saved, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, registry)
    hide = preferences.get(HIDE_CREW.key)
    priority = preferences.get(ORDER.key) == "priority"
    params = {"source": SOURCE}
    if hide:
        params["filter"] = {"op": "not", "filter": {
            "op": "eq", "field": {"token": ROLE}, "value": "crew"}}
    if priority:
        params["sort"] = [{"field": {"token": "herdr_ai_priority"}, "order": "desc"},
                          {"field": "attention", "order": "desc"},
                          {"field": "state_change_seq", "order": "desc"}]
    else:
        params["sort"] = [{"field": field, "order": "asc"}
                          for field in ("workspace_order", "tab_order", "pane_order")]
    return call("agent.view.set", params)


def install_startup(binary):
    """Link a local one-shot startup hook, using this machine's Python path."""
    folder = state_dir() / "herdr-ai-plugin"
    manifest = folder / "herdr-plugin.toml"
    command = [sys.executable, str(Path(__file__).with_name("herdr_ai.py")), "--sync-sidebar"]
    settings = [*command[:-1], "--settings"]
    open_settings = [*command[:-1], "--open-settings"]
    text = ('id = "local.herdr-ai"\nname = "Herdr AI"\nversion = "0.1.0"\n'
            'min_herdr_version = "0.9.3"\ndescription = "Restore crew visibility at server startup"\n'
            'platforms = ["macos", "linux", "windows"]\n\n[[startup]]\ncommand = ' + json.dumps(command) + '\n'
            '\n[[actions]]\nid = "settings"\ntitle = "Herdr AI settings"\n'
            'contexts = ["global"]\ncommand = ' + json.dumps(open_settings) + '\n'
            '\n[[panes]]\nid = "settings"\ntitle = "Herdr AI settings"\n'
            'placement = "popup"\nwidth = "80%"\nheight = "80%"\ncommand = ' + json.dumps(settings) + '\n')
    if manifest.exists() and manifest.read_text(encoding="utf-8") == text:
        import herdr_team_status
        herdr_team_status.ensure_running()
        return
    folder.mkdir(parents=True, exist_ok=True)
    manifest.write_text(text, encoding="utf-8")
    out = subprocess.run([binary, "plugin", "link", str(folder)], capture_output=True, text=True, timeout=5)
    if out.returncode:
        manifest.unlink()  # allow a later invocation to retry
        raise RuntimeError("Could not register Herdr AI startup hook: " + (out.stderr or out.stdout).strip())
    import herdr_team_status
    herdr_team_status.ensure_running()
