#!/usr/bin/env python3
"""rexai: herdr-ai's Standard workbench, laid out in Rex instead of Herdr.

  +--------------+--------------+--------------+
  | Antigravity  | Codex        |              |
  |              |              | Claude       |
  +--------------+--------------+              |
  | agent        | Cursor/Kiro  |              |
  | selector     |              +--------------+
  +--------------+--------------+ usage        |
  | terminal (fs browser)       |              |
  +-----------------------------+--------------+

Every pane starts in the directory you run it from. The five agent columns
are equal widths, with a short fs row and a short usage panel.
The agents launch through this checkout's picker, so models, effort and
permission flags follow your picker defaults, and quitting one reopens the
selector in its pane. The terminal pane runs the fs file browser in icon view
(Shift+Q drops to its shell), opening where fs's "Opens in" setting says.

Run inside Rex from a tab with a single pane and that tab becomes the
workbench, the pane you ran it in becoming the terminal. From a tab that
already has other panes it opens a new tab, and outside Rex a new session.

Herdr-only features stay with herdr-ai: provider teams and Star, refresh in
place, handoff, and the sidebar.

Usage: rexai [--selectors] [--no-fs] [label]
         label        names the tab (defaults to the directory name)
         --selectors  an agent selector in each of the five agent panes, as agent opens
         --no-fs      leave the terminal pane a shell
Env:   REXAI_DRY=1    build the layout with plain shells only
       REX_BIN_PATH   rex executable to use
"""

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENTRY = HERE / "agent_picker.py"
FS_ENTRY = HERE / "fs_browser.py"
SHELL = "com.superlogical.terminal.shell"

# The same panes as herdr-ai's Standard, without --picker-solo: its handoff
# rules restart panes through Herdr, which Rex does not have.
PANES = {
    "agy": ["--picker-return", "--picker-launch", "Antigravity"],
    "codex": ["--picker-return", "--picker-launch", "Codex"],
    "claude": ["--picker-return", "--picker-launch", "Claude"],
    "cursor": ["--picker-return", "--picker-launch-priority"],
    "picker": ["--picker-return"],
    "usage": ["--picker-usage"],
}
AGENT_PANES = ("agy", "codex", "claude", "picker", "cursor")
SELECTORS = {**{role: ["--picker-return"] for role in AGENT_PANES}, "usage": ["--picker-usage"]}
LABELS = {"agy": "Antigravity", "codex": "Codex", "claude": "Claude", "cursor": "Cursor/Kiro",
          "picker": "agent", "usage": "usage", "terminal": "fs"}
SELECTOR_LABELS = {role: "agent" for role in AGENT_PANES}

# Each is the share kept by the left or upper part. Three equal columns, the
# 2x2 agents in equal rows, and a usage panel a little over a quarter of the
# right column, so Claude gets the rest of it. "terminal" is the fallback when
# the tab's height is unknown; otherwise the fs row gets FS_ROWS rows.
SIZES = {"side": 0.6667, "usage": 0.72, "terminal": 0.85, "columns": 0.5, "rows": 0.5}
# fs's large icon tile is 7 rows (ring, icon, two name lines) inside its frame,
# so 11 rows show it whole with a little room around it.
FS_ROWS = 11
# Rows each Rex pane spends on its title bar and gaps (a lone pane of a 74-row
# tab shows 70); a starting estimate that fit_terminal then corrects.
CHROME = 4


class Rex:
    def __init__(self, binary):
        self.binary = binary

    def __call__(self, *args, json_out=True, parse=False):
        argv = [self.binary, *args] + (["--json"] if json_out else [])
        out = subprocess.run(argv, capture_output=True, text=True)
        if out.returncode != 0:
            raise RuntimeError(f"rex {' '.join(args[:2])}: {(out.stderr or out.stdout).strip()}")
        return json.loads(out.stdout) if (json_out or parse) and out.stdout.strip() else {}

    def api(self, session, method, payload):
        argv = [self.binary, "api", "call", "-s", session, method, json.dumps(payload)]
        out = subprocess.run(argv, capture_output=True, text=True)
        if out.returncode != 0:
            raise RuntimeError(f"rex {method}: {(out.stderr or out.stdout).strip()}")
        return json.loads(out.stdout)


def find_rex():
    found = os.environ.get("REX_BIN_PATH") or shutil.which("rex")
    return found if found and os.path.isfile(found) else None


def picker_command(args):
    return shlex.join([sys.executable, str(ENTRY), *args])


def fs_command(cwd):
    """The browser in icon view, starting where fs's Opens in setting points
    for herdr-ai's pane, with cwd as herdr-ai's folder."""
    start = cwd
    try:
        sys.path.insert(0, str(HERE))
        import fs_browser
        start = str(fs_browser.herdr_start(cwd))
    except Exception:
        pass
    return shlex.join([sys.executable, str(FS_ENTRY), "--view", "icon", start])


def browser_wanted(no_fs=False):
    if no_fs or dry() or not FS_ENTRY.is_file():
        return False
    try:
        sys.path.insert(0, str(HERE))
        import team
        return team.terminal_files()
    except Exception:
        return True


def dry():
    return os.environ.get("REXAI_DRY", "0") == "1"


def shell(role, cwd, labels):
    return {"block": {"flavor": SHELL, "label": labels.get(role, role), "options": {"cwd": cwd}}}


def split(left, right, direction, ratio):
    return {"split": {"direction": direction, "ratio": ratio, "before": left, "after": right}}


def block_rect(rex, session, block):
    """The block's [x, y, width, height] as fractions of its tab, or None."""
    def walk(node):
        if node.get("block_id") == block:
            return node.get("rect")
        split = node.get("split") or {}
        return walk(split.get("before") or {}) or walk(split.get("after") or {})
    for window in rex("session", "inspect", session).get("windows", []):
        for layer in window.get("layers", []):
            found = walk(layer.get("layout") or {})
            if found:
                return found
    return None


def tab_rows(rex, session, block):
    """The height in rows of the tab holding block, from block's size as the
    app shows it; None when no app has sized it."""
    try:
        size = rex("block", "call", "-s", session, "-b", block, "size", json_out=False, parse=True)
        rows = size["rows"]
        if not size.get("desired") or not isinstance(rows, int):
            return None
        rect = block_rect(rex, session, block)
        share = rect[3] if rect and rect[3] > 0 else 1
        return round(rows / share)
    except (RuntimeError, KeyError, TypeError, ValueError, IndexError):
        return None


def terminal_ratio(rex, session, root):
    """The agents' share of the left column, leaving fs FS_ROWS rows. A new
    tab no app has drawn yet has the height of the tab rexai ran from; with
    neither, SIZES' fallback."""
    rows = tab_rows(rex, session, root)
    here = os.environ.get("REX_SESSION"), os.environ.get("REX_BLOCK")
    if rows is None and all(here) and here[1] != root:
        rows = tab_rows(rex, *here)
    if not rows or rows < 3 * FS_ROWS:
        return SIZES["terminal"]
    # rows is what one pane shows; the tab is CHROME taller, and fs pays CHROME too.
    return round(1 - (FS_ROWS + CHROME) / (rows + CHROME), 4)


def drawn_rows(rex, session, block):
    """block's height in rows once an app has drawn it, else None."""
    try:
        size = rex("block", "call", "-s", session, "-b", block, "size", json_out=False, parse=True)
        return size["rows"] if size.get("desired") and isinstance(size.get("rows"), int) else None
    except (RuntimeError, KeyError, TypeError, ValueError):
        return None


def fit_terminal(rex, session, panes, ratio, wait=1.5, sleep=time.sleep):
    """Move the border above fs until it has FS_ROWS rows. Each pane's title
    bar and gaps take rows a ratio does not see, so measure and correct. Gives
    up quietly when no app draws the tab within wait seconds: the app draws
    a tab only while it shows it."""
    deadline = time.monotonic() + wait
    for _ in range(4):
        rows = [drawn_rows(rex, session, panes[r]) for r in ("agy", "picker", "terminal")]
        while None in rows and time.monotonic() < deadline:
            sleep(0.5)
            rows = [drawn_rows(rex, session, panes[r]) for r in ("agy", "picker", "terminal")]
        if None in rows or rows[2] == FS_ROWS:
            return ratio
        ratio = round(min(0.95, max(0.5, ratio - (FS_ROWS - rows[2]) / max(1, sum(rows)))), 4)
        rex.api(session, "session.resize_pane", {"session_id": session, "before_block_id": panes["agy"],
                                                 "after_block_id": panes["terminal"], "direction": "vertical",
                                                 "ratio": ratio})
        sleep(0.25)
    return ratio


def spawn_fit(rex, session, panes, ratio):
    """Fit the fs row in the background: an app draws a new tab only once it
    shows it, which can be after rexai has returned."""
    argv = [sys.executable, str(Path(__file__).resolve()), "--fit", rex.binary, session,
            panes["agy"], panes["picker"], panes["terminal"], str(ratio)]
    kwargs = {"creationflags": 0x00000008} if sys.platform == "win32" else {"start_new_session": True}
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, **kwargs)


def fit_main(argv):
    binary, session, agy, picker, terminal, ratio = argv
    fit_terminal(Rex(binary), session, {"agy": agy, "picker": picker, "terminal": terminal},
                 float(ratio), wait=600)
    return 0


def build(rex, session, root, cwd, commands=None, labels=None):
    """Split root into the workbench. root stays the terminal, bottom left.
    Returns {role: block_id}."""
    commands = commands or PANES
    labels = labels or LABELS
    panes = {"terminal": root}
    ratio = terminal_ratio(rex, session, root)   # measured before the splits shrink root
    rex("block", "rename", "-s", session, root, labels.get("terminal", "terminal"), json_out=False)
    # The right column: Claude over usage.
    out = rex.api(session, "session.new_split", {
        "anchor_id": root, "direction": "horizontal", "side": "after", "ratio": SIZES["side"],
        "layout": split(shell("claude", cwd, labels), shell("usage", cwd, labels), "vertical", SIZES["usage"])})
    panes["claude"], panes["usage"] = out["block_ids"]
    # The 2x2 grid above the terminal: Antigravity and the selector on the left,
    # Codex and Cursor/Kiro on the right.
    rows = SIZES["rows"]
    grid = split(split(shell("agy", cwd, labels), shell("picker", cwd, labels), "vertical", rows),
                 split(shell("codex", cwd, labels), shell("cursor", cwd, labels), "vertical", rows),
                 "horizontal", SIZES["columns"])
    out = rex.api(session, "session.new_split", {
        "anchor_id": root, "direction": "vertical", "side": "before", "ratio": ratio,
        "layout": grid})
    panes["agy"], panes["picker"], panes["codex"], panes["cursor"] = out["block_ids"]
    if not dry():
        spawn_fit(rex, session, panes, ratio)
        for role, args in commands.items():
            type_line(rex, session, panes[role], picker_command(args))
    return panes


def type_line(rex, session, block, line):
    """Type line at the block's shell and press Return, as herdr-ai's pane run
    does, so quitting the program leaves the pane at its shell."""
    rex("send", "-s", session, "-b", block, line, json_out=False)
    rex("send-key", "-s", session, "-b", block, "enter", json_out=False)


def window_blocks(rex, session, window):
    for w in rex("session", "inspect", session).get("windows", []):
        if w.get("window_id") == window:
            return [b["block_id"] for layer in w.get("layers", []) for b in layer.get("blocks", [])]
    return []


def main(argv=None, rex=None, cwd=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] in (["-h"], ["--help"]):
        print(__doc__.strip())
        return 0
    if argv[:1] == ["--fit"]:
        return fit_main(argv[1:])
    no_fs, selectors = "--no-fs" in argv, "--selectors" in argv
    argv = [a for a in argv if a not in ("--no-fs", "--selectors")]
    if argv and argv[0].startswith("-"):
        sys.exit(f"rexai: unknown option {argv[0]}; see rexai --help")
    if len(argv) > 1:
        sys.exit("rexai: takes one label; quote it if it has spaces")
    if rex is None:
        binary = find_rex()
        if not binary:
            sys.exit("rexai: rex not found; open Rex once, or set REX_BIN_PATH")
        rex = Rex(binary)
    cwd = cwd or os.getcwd()
    label = argv[0] if argv else (Path(cwd).name or "home")
    commands, labels = (SELECTORS, {**LABELS, **SELECTOR_LABELS}) if selectors else (PANES, LABELS)
    fs = browser_wanted(no_fs)

    session, block = os.environ.get("REX_SESSION"), os.environ.get("REX_BLOCK")
    here = None
    if session and block:
        try:
            where = rex("whoami")
            window = where.get("window_id")
            if where.get("block_id") == block and window and window_blocks(rex, session, window) == [block]:
                here = window
        except RuntimeError:
            session = None
    in_place = bool(here) and sys.stdin.isatty() and sys.stdout.isatty()

    if in_place:
        window, root = here, block
        rex("window", "rename", "-s", session, window, label, json_out=False)
    elif session:
        out = rex("window", "new", "-s", session, label, "-c", cwd)
        window, root = out["window_id"], out["block_ids"][0]
    else:
        out = rex("new", label, "-c", cwd, "--window", label)
        session = out["session_id"]
        first = out["initial_windows"][0]
        window, root = first["window_id"], first["block_ids"][0]

    build(rex, session, root, cwd, commands, labels)
    print(f"rexai: laid out {label} in {cwd}")
    if in_place:
        if fs:
            sys.stdout.flush()
            line = shlex.split(fs_command(cwd))
            if sys.platform == "win32":
                return subprocess.call(line)
            os.execv(line[0], line)
        return 0
    if fs:
        type_line(rex, session, root, fs_command(cwd))
    if not os.environ.get("REX_SESSION"):
        try:
            rex("do", "session.select", f"session_id={session}", f"window_id={window}", json_out=False)
        except RuntimeError:
            print(f"rexai: open session {label} in Rex to see it")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        sys.exit(f"rexai: {exc}")
