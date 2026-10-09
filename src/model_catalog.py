"""Public model choices from provider catalogs, with a bundled offline fallback."""

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time

from platform_runtime import resolve_argv, state_dir

ROOT = Path(__file__).resolve().parents[1]
PATH = state_dir() / "models.json"
LOCK = threading.Lock()
PENDING = set()
_live = {}
SCHEMA_VERSION = 3
EFFORT = re.compile(r"-(minimal|none|low|medium|high|xhigh|max|ultra)(?=-fast$|$)")


def read(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def cursor_models(output):
    grouped = {}
    for line in output.splitlines():
        match = re.match(r"^([a-z0-9][a-z0-9_.-]*) - (.+)$", line)
        if not match:
            continue
        model, label = match.groups()
        effort = EFFORT.search(model)
        base = EFFORT.sub("", model)
        display = re.sub(r"\s+\((?:Extra High|High|Medium|Low|Max|Ultra|Minimal|None)\)$", "", label)
        display = re.sub(r"\s+(?:Extra High|High|Medium|Low|Max|Ultra|Minimal|None)\b", "", display).replace(" (default)", "").strip()
        row = grouped.setdefault(base, {"id": base, "name": display, "efforts": [], "variants": {}})
        if effort:
            level = effort.group(1)
            row["efforts"].append(level)
            row["variants"][level] = model
        else:
            row["name"] = label.removesuffix(" (default)") if hasattr(label, "removesuffix") else label.replace(" (default)", "")
            row["variants"]["provider default"] = model
    order = ("minimal", "none", "low", "medium", "high", "xhigh", "max", "ultra")
    for row in grouped.values():
        row["efforts"] = sorted(set(row["efforts"]), key=lambda e: order.index(e))
    return list(grouped.values())


def claude_models(folder):
    """Prefer the per-account catalog Claude Code refreshes often, then the published document."""
    rows, best = [], (-1, 0)
    for path in folder.glob("*.json"):
        data = read(path)
        config = data.get("catalog", {}).get("config", {}) if isinstance(data.get("catalog"), dict) else {}
        models = config.get("models") if isinstance(config, dict) else None
        if not isinstance(models, list) or not models:
            continue
        rank = (config.get("id") == "cc", data.get("fetchedAt") or 0)
        if rank > best:
            best = rank
            rows = [{"id": m["id"], "name": m.get("name", m["id"]),
                     "efforts": [e["id"] for e in (m.get("thinking") or {}).get("effort_options", []) if e.get("id")]}
                    for m in models if isinstance(m, dict) and m.get("id")]
    if rows:
        return rows
    files = sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files:
        document = read(path).get("document", {})
        if not isinstance(document, dict):
            continue
        configs = document.get("surfaces", {}).get("cc", {}).get("model_selector_config", [])
        models = [m for config in configs for m in config.get("models", [])]
        if models:
            return [{"id": m["id"], "name": m.get("name", m["id"]),
                     "efforts": m.get("runtime", {}).get("effort_levels") or []}
                    for m in models if m.get("id")]
    return []


def fetch(agent):
    name = agent["name"]
    if name == "Codex":
        path = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "models_cache.json"
        return [{"id": m["slug"], "name": m.get("display_name", m["slug"]),
                 "efforts": [e["effort"] for e in m.get("supported_reasoning_levels", [])]}
                for m in read(path).get("models", []) if m.get("visibility") == "list" and m.get("slug")]
    if name == "Claude":
        return claude_models(Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))) / "cache/model-catalog")
    argv = resolve_argv(agent)
    if not argv:
        return []
    commands = {"Cursor": ["--list-models"], "Kiro": ["chat", "--list-models", "--format", "json"], "Antigravity": ["models"]}
    if name not in commands:
        return []
    result = subprocess.run([argv[0]] + commands[name], capture_output=True, text=True, timeout=12, encoding="utf-8")
    if result.returncode:
        return []
    if name == "Cursor":
        return cursor_models(result.stdout)
    if name == "Kiro":
        return [{"id": m["model_id"], "name": m.get("model_name", m["model_id"]),
                 "efforts": ["low", "medium", "high", "xhigh", "max"] if m["model_id"] != "auto" else []}
                for m in json.loads(result.stdout).get("models", []) if m.get("model_id")]
    rows = cursor_models("\n".join(line.replace("\t", " - ", 1) for line in result.stdout.splitlines() if "\t" in line))
    for row in rows:
        if not row["efforts"]:
            row["efforts"] = None
    return rows


def cached_entry(name):
    """name's entry in the refresh cache, or {} when the cache is stale or malformed."""
    stored = read(PATH)
    cached = stored.get(name) if stored.get("schema_version") == SCHEMA_VERSION else None
    if not isinstance(cached, dict):
        return {}
    return {"models": cached["models"] if isinstance(cached.get("models"), list) else [],
            "fetched": cached["fetched"] if isinstance(cached.get("fetched"), (int, float)) else 0}


def load_rows(agent):
    name = agent["name"]
    rows = _live.get(name) or cached_entry(name).get("models") or read(ROOT / "models.json").get(name)
    if not isinstance(rows, list):
        rows = []
    return [dict(row) for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)]


_EFFORT_TITLE = {"minimal": "Minimal", "none": "None", "low": "Low", "medium": "Medium",
                 "high": "High", "xhigh": "Extra High", "max": "Max", "ultra": "Ultra"}


def expand_gemini(rows):
    """List each Gemini effort as its own model. The id already carries the level."""
    expanded = []
    for row in rows:
        variants = row.get("variants") if isinstance(row.get("variants"), dict) else {}
        efforts = row.get("efforts") if isinstance(row.get("efforts"), list) else []
        levels = [level for level in efforts if isinstance(level, str) and isinstance(variants.get(level), str)]
        if not str(row.get("id", "")).startswith("gemini") or not levels:
            expanded.append(row)
            continue
        base = re.sub(r"\s*\((?:Extra High|High|Medium|Low|Max|Ultra|Minimal|None)\)$", "",
                      row.get("name") or row["id"]).strip()
        seen = set()
        for level in levels:
            model_id = variants[level]
            if model_id in seen:
                continue
            seen.add(model_id)
            expanded.append({"id": model_id, "name": f"{base} ({_EFFORT_TITLE.get(level, level)})",
                             "efforts": [], "variants": {}})
        default = variants.get("provider default")
        if isinstance(default, str) and default not in seen:
            expanded.append({"id": default, "name": base, "efforts": [], "variants": {}})
    return expanded


def options(agent, current):
    rows = expand_gemini(load_rows(agent))
    if current and not any(row["id"] == current for row in rows):
        rows.insert(0, {"id": current, "name": current + " (current)",
                        "efforts": [] if str(current).startswith("gemini") else None})
    return rows


def request(agent):
    """Refresh off the UI thread, keeping startup and arrow input responsive."""
    if os.environ.get("AGENT_PICKER_NO_MODEL_REFRESH") == "1":
        return
    name = agent["name"]
    with LOCK:
        if name in PENDING or time.time() - cached_entry(name).get("fetched", 0) < 3600:
            return
        PENDING.add(name)
    def worker():
        try:
            models = fetch(agent)
            if not models:
                return
            with LOCK:
                _live[name] = models
                data = read(PATH)
                if data.get("schema_version") != SCHEMA_VERSION:
                    data = {"schema_version": SCHEMA_VERSION}
                data[name] = {"models": models, "fetched": time.time()}
                PATH.parent.mkdir(parents=True, exist_ok=True)
                fd, temp = tempfile.mkstemp(prefix=".models-", dir=PATH.parent)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as stream:
                        json.dump(data, stream, indent=2)
                    os.replace(temp, PATH)
                finally:
                    if os.path.exists(temp):
                        os.unlink(temp)
        except Exception:   # a malformed CLI cache must not print over the picker
            pass
        finally:
            with LOCK:
                PENDING.discard(name)
    threading.Thread(target=worker, daemon=True).start()
