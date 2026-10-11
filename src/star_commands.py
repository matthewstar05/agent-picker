"""Provider commands and skills, by the names their own interfaces accept.

A native pane runs `/deep-research topic` or `/design:ux-copy` itself. StarCLI
hands the same instructions to whichever provider takes the turn, so a command
keeps working when routing moves to another provider. Built-in Star commands
win over these names.
"""

import json
import re
import shlex
from pathlib import Path

FRONTMATTER = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.S)


def _skills(root, prefix=""):
    """name -> SKILL.md under root/<name>/, or a synced copy such as
    root/synced/<account>/<name>/. The shallowest one wins."""
    out = {}
    for depth in ("*/", "*/*/", "*/*/*/"):
        for path in sorted(root.glob(depth + "SKILL.md")):
            out.setdefault(prefix + path.parent.name, path)
    return out


def _templates(root, prefix=""):
    """name -> markdown command; a subfolder adds a namespace, as `dir:name`."""
    out = {}
    for path in sorted(root.rglob("*.md")) if root.is_dir() else ():
        parts = path.relative_to(root).with_suffix("").parts
        out[prefix + ":".join(parts)] = path
    return out


def _plugins(home):
    out = {}
    roots = list((home / ".claude/plugins/synced").glob("*/*/.claude-plugin/plugin.json"))
    roots += list((home / ".claude/plugins/cache").glob("*/*/*/.claude-plugin/plugin.json"))
    for manifest in sorted(roots):
        folder = manifest.parent.parent
        try:
            name = json.loads(manifest.read_text(encoding="utf-8")).get("name") or folder.name
        except (OSError, ValueError, AttributeError):
            name = folder.name
        for key, path in _skills(folder / "skills").items():
            out.setdefault(f"{name}:{key}", ("skill", path))
        for key, path in _templates(folder / "commands").items():
            out.setdefault(f"{name}:{key}", ("command", path))
    return out


def find(cwd, home=None):
    """{name: (kind, path)}: user entries, then the project's, which win."""
    home = Path(home or Path.home())
    found = {}
    plugins = _plugins(home)
    found.update(plugins)
    # A plugin skill also answers to its bare name when no other plugin has it.
    bare = {}
    for name in plugins:
        bare.setdefault(name.split(":", 1)[1], []).append(name)
    for short, full in bare.items():
        if len(full) == 1:
            found.setdefault(short, plugins[full[0]])
    for base in (home, Path(cwd)):
        for folder in (".claude", ".codex", ".cursor", ".kiro", ".agents"):
            for name, path in _skills(base / folder / "skills").items():
                found[name] = ("skill", path)
        for folder, prefix in ((".claude/commands", ""), (".cursor/commands", ""), (".codex/prompts", "prompts:")):
            for name, path in _templates(base / folder, prefix).items():
                found[name] = ("command", path)
    return found


def expand(kind, path, arguments):
    """The prompt a provider follows for `/name arguments`."""
    arguments = arguments.strip()
    if kind == "skill":
        return (f"[StarCLI] The user invoked the skill at {path}. Read that file and follow its "
                f"instructions; files it mentions are relative to {path.parent}.\n\n"
                f"Request: {arguments or '(none given)'}")
    text = FRONTMATTER.sub("", path.read_text(encoding="utf-8"), count=1).strip()
    try:
        words = shlex.split(arguments)
    except ValueError:
        words = arguments.split()
    used = bool(re.search(r"\$(?:ARGUMENTS|[1-9])", text))

    def fill(match):
        # One pass, so a "$1" typed in the arguments is not expanded again.
        if match.group(0) == "$ARGUMENTS":
            return arguments
        n = int(match.group(1))
        return words[n - 1] if n <= len(words) else ""
    text = re.sub(r"\$ARGUMENTS|\$([1-9])", fill, text)
    if arguments and not used:
        text += "\n\nARGUMENTS: " + arguments
    return text


def describe(kind, path):
    """The frontmatter description a native menu shows, else the kind."""
    try:
        with open(path, encoding="utf-8") as handle:
            head = handle.read(4096)
    except OSError:
        return kind
    match = FRONTMATTER.match(head if head.endswith("\n") else head + "\n")
    line = re.search(r"^description:\s*[\"']?(.+?)[\"']?\s*$", match.group(0) if match else "", re.M)
    return line.group(1).strip() if line else kind
