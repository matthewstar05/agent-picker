"""Dispatch to the installer for the current platform."""

import importlib.util
from pathlib import Path
import sys


def main(argv=None):
    if sys.platform == 'win32':
        name = 'install-windows.py'
    elif sys.platform == 'darwin':
        name = 'install-macos.py'
    else:
        raise SystemExit('agent-picker installation supports Windows and macOS.')
    scripts_dir = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location('agent_picker_platform_installer', scripts_dir / name)
    module = importlib.util.module_from_spec(spec)
    original_path = sys.path[:]
    sys.path.insert(0, str(scripts_dir))
    try:
        spec.loader.exec_module(module)
        return module.main(argv)
    finally:
        sys.path[:] = original_path


if __name__ == '__main__':
    sys.exit(main())
