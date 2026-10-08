"""Editable launch defaults, stored only in the machine's picker config."""

import copy
import json
import os
import re
import tempfile
import model_catalog


EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
PROVIDERS = {
    "Claude": ("--model", "--effort", ("--dangerously-skip-permissions",)),
    "Codex": ("-m", None, ("--dangerously-bypass-approvals-and-sandbox",)),
    "Antigravity": ("--model", "--effort", ("--dangerously-skip-permissions",)),
    "Cursor": ("--model", None, ("--yolo", "--force", "-f", "--trust")),
    "Kiro": ("--model", "--effort", ("--trust-all-tools", "-a")),
}
CURSOR_EFFORT = re.compile(r"-(low|medium|high|xhigh|max|minimal|none)(?=-fast$|$)")
GEMINI_ENCODED = re.compile(r"^gemini-.+-(?:minimal|none|low|medium|high|xhigh|max|ultra)(?:-fast)?$")


def option(argv, flags, default=""):
    for i, arg in enumerate(argv[1:], 1):
        for flag in flags:
            if arg == flag and i + 1 < len(argv):
                default = argv[i + 1]
            elif arg.startswith(flag + "="):
                default = arg[len(flag) + 1:]
    return default


def replace_option(argv, flags, value, preferred):
    out = [argv[0]]
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg in flags:
            i += 2
        elif any(arg.startswith(flag + "=") for flag in flags):
            i += 1
        else:
            out.append(arg)
            i += 1
    if value:
        out.extend([preferred, value])
    return out


def codex_configs(argv):
    """Yield (start, end, key, value) for Codex CLI config overrides."""
    i = 1
    while i < len(argv):
        arg, end = argv[i], i + 1
        value = ""
        if arg in ("-c", "--config") and end < len(argv):
            value, end = argv[end], end + 1
        elif arg.startswith("--config="):
            value = arg[len("--config="):]
        elif arg.startswith("-c") and arg != "-c":
            value = arg[2:]
        if "=" in value:
            key, value = value.split("=", 1)
            yield i, end, key.strip(), value.strip().strip('"\'')
        i = end


def current(agent):
    name, argv = agent["name"], agent["argv"]
    if name not in PROVIDERS:
        return None
    model_flag, effort_flag, bypass_flags = PROVIDERS[name]
    model = option(argv, (model_flag, "--model"))
    effort = option(argv, (effort_flag,)) if effort_flag else ""
    if name == "Codex":
        for i, arg in enumerate(argv):
            value = argv[i + 1] if arg in ("-c", "--config") and i + 1 < len(argv) else ""
            if value.startswith("model_reasoning_effort="):
                effort = value.split("=", 1)[1].strip('"\'')
    if name in ("Cursor", "Antigravity"):
        if GEMINI_ENCODED.match(model):
            effort = "provider default"
        else:
            match = CURSOR_EFFORT.search(model)
            bracket = re.search(r"effort=([^,\]]+)", model)
            if name == "Cursor" or not effort:
                effort = match.group(1) if match else bracket.group(1) if bracket else "provider default"
            if match:
                model = model[:match.start()] + model[match.end():]
    values = {"model": model, "effort": effort or "provider default",
              "bypass_permissions": any(a in bypass_flags for a in argv[1:])}
    if name == "Codex":
        config = {key: value for _, _, key, value in codex_configs(argv)}
        values["fast_mode"] = config.get("service_tier") in ("fast", "priority")
    return values


def apply(agent, values):
    """Change known flags while preserving executable paths and unrelated args."""
    baseline = current(agent)
    if baseline is None:
        return agent
    set_fast = agent["name"] == "Codex" and "fast_mode" in values
    values = dict(baseline, **values)
    model, effort = values["model"], values["effort"]
    if not isinstance(model, str) or not model.strip() or any(ord(c) < 32 for c in model):
        raise ValueError(f"{agent['name']}: model must be a nonempty model identifier")
    if effort not in EFFORTS + ("provider default", "minimal", "none"):
        raise ValueError(f"{agent['name']}: unknown effort {effort}")
    if not isinstance(values["bypass_permissions"], bool):
        raise ValueError("Bypass Permissions must be on or off")
    if agent["name"] == "Codex" and not isinstance(values["fast_mode"], bool):
        raise ValueError("Fast Mode must be on or off")
    name = agent["name"]
    if name == "Antigravity" and not str(model).startswith("gemini"):
        model = CURSOR_EFFORT.sub("", model)
    model_flag, effort_flag, bypass_flags = PROVIDERS[name]
    argv = list(agent["argv"])
    if name in ("Cursor", "Antigravity") and (model != baseline["model"] or effort != baseline["effort"]):
        row = next((r for r in model_catalog.load_rows(agent) if r["id"] == model), {})
        variant = row.get("variants", {}).get(effort)
        if not variant and effort == "provider default" and not str(model).startswith("gemini"):
            variant = row.get("variants", {}).get("high")
        if variant:
            model = variant
        elif effort != "provider default" and name == "Cursor" and not str(model).startswith("gemini"):
            match = CURSOR_EFFORT.search(model)
            if match:
                model = model[:match.start()] + model[match.end():]
            if re.search(r"effort=[^,\]]+", model):
                model = re.sub(r"effort=[^,\]]+", "effort=" + effort, model)
            elif model.endswith("]"):
                model = model[:-1] + ",effort=" + effort + "]"
            else:
                fast = "-fast" if model.endswith("-fast") else ""
                model = model[:-5] if fast else model
                model += "-" + effort + fast
        argv = replace_option(argv, (model_flag,), model, model_flag)
    elif model != baseline["model"]:
        argv = replace_option(argv, (model_flag, "--model"), model, model_flag)
    if GEMINI_ENCODED.match(model):
        if effort_flag:
            argv = replace_option(argv, (effort_flag,), "", effort_flag)
        effort = "provider default"
    elif effort != baseline["effort"] and name == "Codex":
        out, i = [argv[0]], 1
        while i < len(argv):
            if argv[i] in ("-c", "--config") and i + 1 < len(argv) and argv[i + 1].startswith("model_reasoning_effort="):
                i += 2
            else:
                out.append(argv[i])
                i += 1
        argv = out
        if effort != "provider default":
            argv.extend(["-c", "model_reasoning_effort=" + effort])
    elif effort_flag and effort != baseline["effort"]:
        argv = replace_option(argv, (effort_flag,), "" if effort == "provider default" else effort, effort_flag)
    if values["bypass_permissions"] != baseline["bypass_permissions"]:
        argv = [a for a in argv if a not in bypass_flags]
        if name == "Claude":
            argv = replace_option(argv, ("--permission-mode",), "" if values["bypass_permissions"] else "default", "--permission-mode")
        elif name == "Codex":
            argv = replace_option(argv, ("-a", "--ask-for-approval"), "" if values["bypass_permissions"] else "on-request", "--ask-for-approval")
            argv = replace_option(argv, ("-s", "--sandbox"), "" if values["bypass_permissions"] else "workspace-write", "--sandbox")
        if values["bypass_permissions"]:
            argv.append(bypass_flags[0])
            if name == "Cursor":
                argv.append("--trust")
    if set_fast:
        remove = set()
        for start, end, key, _ in codex_configs(argv):
            if key in ("service_tier", "features.fast_mode"):
                remove.update(range(start, end))
        argv = [arg for i, arg in enumerate(argv) if i not in remove]
        # Keep /fast available in Codex; the service tier controls this launch.
        argv.extend(["-c", "features.fast_mode=true", "-c",
                     "service_tier=" + ("fast" if values["fast_mode"] else "default")])
    values["model"] = model
    values["effort"] = effort
    agent["argv"] = argv
    agent["defaults"] = values
    agent["model"] = model
    agent["short"] = model
    agent["detail"] = model if str(model).startswith("gemini") else model + " · " + effort
    if name == "Codex" and values["fast_mode"]:
        agent["detail"] += " · fast"
    return agent


def save(agent, values, path):
    """Merge local settings atomically; never pin the shared executable or argv."""
    updated = apply(copy.deepcopy(agent), values)
    data = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    data.setdefault("agents", {}).setdefault(agent["name"], {})["defaults"] = dict(values)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".defaults-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(data, indent=2) + "\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    agent.clear()
    agent.update(updated)


class Menu:
    LABELS = {"model": "Model", "effort": "Effort", "bypass_permissions": "Bypass Permissions",
              "fast_mode": "Fast Mode"}

    def __init__(self, agent):
        self.agent = agent
        self.original = current(agent)
        self.values = dict(self.original) if self.original else {}
        self.index = 0
        self.model_rows = model_catalog.options(agent, self.values.get("model", ""))
        self.error = ""
        self.browsing = False
        self.model_index = 0
        model_catalog.request(agent)

    @property
    def changed(self):
        return self.original is not None and self.values != self.original

    @staticmethod
    def shown(key, value):
        return ("On" if value else "Off") if key in ("bypass_permissions", "fast_mode") else str(value)

    def review(self):
        """(label, new) for the model and effort to keep, then (label, old, new) for each change."""
        keys = ["model"]
        if "effort" in self.fields() or self.original.get("effort") != self.values.get("effort"):
            keys.append("effort")
        selection = [(self.LABELS[k], self.shown(k, self.values[k])) for k in keys]
        delta = [(self.LABELS[k], self.shown(k, self.original[k]), self.shown(k, self.values[k]))
                 for k in self.values if self.original[k] != self.values[k]]
        return selection, delta

    def fields(self):
        if self.agent["name"] == "Codex":
            return ("model", "effort", "bypass_permissions", "fast_mode")
        if str(self.values.get("model", "")).startswith("gemini"):
            return ("model", "bypass_permissions")
        return ("model", "effort", "bypass_permissions")

    def models(self):
        return self.model_rows

    def efforts(self):
        row = next((r for r in self.models() if r["id"] == self.values["model"]), {})
        levels = row.get("efforts")
        choices = tuple(levels) if levels is not None else (EFFORTS if self.agent["name"] == "Codex" else EFFORTS[:-1])
        variants = row.get("variants", {})
        if self.agent["name"] not in ("Cursor", "Antigravity") or not variants or "provider default" in variants or not choices:
            choices = ("provider default",) + choices
        return choices

    def select_model(self, row):
        self.values["model"] = row["id"]
        if str(row["id"]).startswith("gemini"):
            self.values["effort"] = "provider default"
        else:
            choices = self.efforts()
            if self.values["effort"] not in choices:
                self.values["effort"] = "high" if "high" in choices else choices[0]
        self.index = min(self.index, len(self.fields()) - 1)

    def handle(self, key):
        self.error = ""
        if self.original is None:
            return "save" if key == "esc" else None
        if self.browsing:
            models = self.models()
            self.model_index = min(self.model_index, len(models) - 1)
            if key == "esc":
                self.browsing = False
                return "save"
            if key in ("up", "left", "k", "down", "right", "j", "\t"):
                step = -1 if key in ("up", "left", "k") else 1
                self.model_index = (self.model_index + step) % len(models)
            elif key in ("\r", "\n", " "):
                self.select_model(models[self.model_index])
                self.browsing = False
            return None
        self.index = min(self.index, len(self.fields()) - 1)
        if key == "esc":
            return "save"
        elif key in ("up", "k", "down", "j", "\t"):
            self.index = (self.index + (-1 if key in ("up", "k") else 1)) % len(self.fields())
        elif key in ("left", "right", "\r", "\n", " "):
            field = self.fields()[self.index]
            if field == "model":
                if key in ("\r", "\n", " "):
                    models = self.models()
                    self.browsing = True
                    self.model_index = next((i for i, row in enumerate(models) if row["id"] == self.values["model"]), 0)
            elif field == "effort":
                choices = self.efforts()
                effort = self.values[field]
                step = -1 if key == "left" else 1
                self.values[field] = choices[(choices.index(effort) + step) % len(choices)] if effort in choices else choices[0]
            else:
                self.values[field] = not self.values[field]
        return None
