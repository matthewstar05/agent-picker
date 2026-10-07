"""Shared launch profiles plus machine-local overrides."""

import provider_defaults
import json
import os
from pathlib import Path
from platform_runtime import WINDOWS

ROOT = Path(__file__).resolve().parents[1]


def config_path():
    if os.environ.get("AGENT_PICKER_CONFIG"):
        return Path(os.environ["AGENT_PICKER_CONFIG"]).expanduser()
    base = (os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming"))
            if WINDOWS else os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return Path(base) / "agent-picker/config.json"


def load_agents():
    agents = json.loads((ROOT / "agents.json").read_text(encoding="utf-8"))
    path = config_path()
    overrides = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    if not isinstance(overrides, dict) or not isinstance(overrides.get("agents", {}), dict):
        raise ValueError("local config must contain an 'agents' object")
    unknown = set(overrides.get("agents", {})) - {a["name"] for a in agents}
    if unknown:
        raise ValueError("unknown agent overrides: " + ", ".join(sorted(unknown)))
    platform = "windows" if WINDOWS else "posix"
    for agent in agents:
        agent.update(agent.pop("platforms", {}).get(platform, {}))
        agent.update(overrides.get("agents", {}).get(agent["name"], {}))
        command = agent.pop("command", None)
        if command is not None:
            if not isinstance(command, str) or not command:
                raise ValueError(f"{agent['name']}: command must be a nonempty string")
            agent["argv"] = [command] + agent["argv"][1:]
            agent["executables"] = []  # explicit local path must not silently fall back
        argv = agent.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
            raise ValueError(f"{agent['name']}: argv must be a nonempty array of strings")
        if Path(argv[0]).stem.lower() == "agent" and not Path(argv[0]).is_absolute():
            raise ValueError("Cursor's generic 'agent' must be an absolute vendor path to prevent recursion")
        if "defaults" in agent:
            provider_defaults.apply(agent, agent["defaults"])
        # Names it appears under in Herdr and in process lists, for the usage screen.
        names = agent.get("detect") or [Path(argv[0]).stem.lower()]
        agent["detect"] = [n.lower() for n in names]
    return agents


def update_checkout():
    import subprocess
    if not (ROOT / ".git").exists():
        raise RuntimeError("Install from a Git clone to use --picker-update.")
    dirty = subprocess.check_output(["git", "-C", str(ROOT), "status", "--porcelain"], text=True)
    if dirty:
        raise RuntimeError("Checkout has local changes. Commit and push them, or resolve them before updating.")
    subprocess.run(["git", "-C", str(ROOT), "pull", "--ff-only"], check=True)
    print("Updated. The next agent command uses this checkout's current code.")
