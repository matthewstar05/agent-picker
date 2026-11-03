"""Install and exercise launchers in a disposable user profile, never the real one."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix="picker-install-") as directory:
        home = Path(directory) / "new user"
        home.mkdir()
        env = dict(os.environ, HOME=str(home), USERPROFILE=str(home),
                   LOCALAPPDATA=str(home / "AppData/Local"), APPDATA=str(home / "AppData/Roaming"),
                   AGENT_PICKER_STATE_HOME=str(home / "state"), AGENT_PICKER_CONFIG=str(home / "config.json"),
                   AGENT_PICKER_NO_PRETRUST="1")
        if sys.platform == "win32":
            bindir = home / "commands"
            subprocess.run([sys.executable, str(ROOT / "scripts/install-windows.py"), "--no-path", "--prefix", str(bindir)],
                           env=env, check=True, timeout=180)
            launcher = bindir / "agent.cmd"
        else:
            subprocess.run([sys.executable, str(ROOT / "scripts/install-macos.py")], env=env, check=True, timeout=30)
            launcher = home / ".local/bin/agent-picker"
        subprocess.run([str(launcher), "--picker-doctor"], env=env, check=True, timeout=30)
        marker = home / "received.json"
        fake = home / "fake provider.py"
        fake.write_text("import json, sys\nfrom pathlib import Path\nPath(sys.argv[1]).write_text(json.dumps(sys.argv[2:]), encoding='utf-8')\nraise SystemExit(7)\n", encoding="utf-8")
        (home / "config.json").write_text(json.dumps({"agents": {"Claude": {"argv": [sys.executable, str(fake), str(marker)]}}}), encoding="utf-8")
        arguments = ["a prompt with spaces", "--user-flag"]
        result = subprocess.run([str(launcher), "--picker-launch", "Claude", "--", *arguments], env=env, timeout=30)
        assert result.returncode == 7, result.returncode
        assert json.loads(marker.read_text(encoding="utf-8")) == arguments
    print("Isolated launcher installation, provider arguments and exit status passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
