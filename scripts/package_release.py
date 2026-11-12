"""Build the release files: one bundle per platform, checksums, Herdr's update manifest.

    python scripts/package_release.py bundle --target macos-aarch64 \
        --binary herdr-macos-aarch64 --version 2.0.0 --out dist
    python scripts/package_release.py manifest --dir dist --tag v2.0.0

A bundle is this repository as tracked by Git, without the Herdr source, the
tests and the CI files, plus bin/herdr built from herdr/. On Windows --binary is
the zip Herdr's packager writes (herdr.exe and its ConPTY files), unpacked into
bin. manifest then writes SHA256SUMS.txt for every file in the folder, and
herdr-latest.json, which the bundled Herdr reads for `herdr update`.
"""

import argparse
import hashlib
import io
import json
import re
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "matthewstar05/agent-picker"
LEFT_OUT = ("herdr/", "tests/", ".github/")
TARGETS = ("macos-aarch64", "macos-x86_64", "windows-x86_64")
NOTICE = """This folder holds a build of Herdr (https://github.com/herdrdev/herdr), licensed
under the Apache License 2.0; see HERDR-LICENSE. It is built from the herdr/
folder of https://github.com/{repository}, which carries changes of its own and
is not an official Herdr release.
"""


def tracked(root=ROOT):
    """Paths Git tracks that belong in a bundle."""
    out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], check=True, capture_output=True).stdout
    names = [name for name in out.decode("utf-8").split("\0") if name]
    return [name for name in names if not name.startswith(LEFT_OUT)]


def herdr_facts(root=ROOT):
    """(version, protocol, endpoint generation) read from the Herdr source."""
    def grab(path, pattern):
        match = re.search(pattern, (root / "herdr" / path).read_text(encoding="utf-8"), re.M)
        if not match:
            raise SystemExit(f"package_release: could not read {pattern!r} from herdr/{path}")
        return match[1]
    return (grab("Cargo.toml", r'^version\s*=\s*"([^"]+)"'),
            int(grab("src/protocol/wire.rs", r"pub const PROTOCOL_VERSION: u32 = (\d+);")),
            int(grab("src/protocol/endpoint.rs", r"pub const ENDPOINT_PROTOCOL_GENERATION: u32 = (\d+);")))


def bundle(target, binary, version, out, root=ROOT):
    if target not in TARGETS:
        raise SystemExit(f"package_release: unknown target {target}; known: {', '.join(TARGETS)}")
    binary, out = Path(binary), Path(out)
    out.mkdir(parents=True, exist_ok=True)
    windows = target.startswith("windows")
    extra = {"VERSION": (version + "\n").encode(),
             "bin/HERDR-LICENSE": (root / "herdr" / "LICENSE").read_bytes(),
             "bin/HERDR-NOTICE.txt": NOTICE.format(repository=REPOSITORY).encode()}
    programs = {}     # paths that must be executable
    if windows:
        with zipfile.ZipFile(binary) as packed:
            for info in packed.infolist():
                if not info.is_dir():
                    extra["bin/" + info.filename.replace("\\", "/")] = packed.read(info)
        if "bin/herdr.exe" not in extra:
            raise SystemExit(f"package_release: {binary} holds no herdr.exe")
    else:
        extra["bin/herdr"] = binary.read_bytes()
        programs["bin/herdr"] = True
    names = tracked(root)
    if windows:
        path = out / f"agent-picker-{target}.zip"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as packed:
            for name in names:
                packed.write(root / name, name)
            for name, data in extra.items():
                packed.writestr(name, data)
    else:
        path = out / f"agent-picker-{target}.tar.gz"
        with tarfile.open(path, "w:gz") as packed:
            for name in names:
                packed.add(root / name, name, recursive=False)
            for name, data in sorted(extra.items()):
                info = tarfile.TarInfo(name)
                info.size, info.mode = len(data), 0o755 if name in programs else 0o644
                packed.addfile(info, io.BytesIO(data))
    return path


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def manifest(folder, tag, root=ROOT):
    """Write herdr-latest.json and SHA256SUMS.txt beside the release files."""
    folder = Path(folder)
    version, protocol, generation = herdr_facts(root)
    base = f"https://github.com/{REPOSITORY}/releases/download/{tag}/"
    assets, sums = {}, {}
    for target in TARGETS:
        name = f"herdr-{target}" + (".zip" if target.startswith("windows") else "")
        if (folder / name).is_file():
            assets[target], sums[target] = base + name, digest(folder / name)
    notes = f"Herdr {version} as built for agent-picker {tag}. Update the whole install with agent --picker-update."
    release = {"notes": notes, "protocol": protocol, "endpoint_generation": generation,
               "assets": assets, "sha256": sums}
    (folder / "herdr-latest.json").write_text(
        json.dumps({"version": version, **release, "releases": {version: release}}, indent=2) + "\n",
        encoding="utf-8")
    lines = [f"{digest(path)}  {path.name}" for path in sorted(folder.iterdir())
             if path.is_file() and path.name != "SHA256SUMS.txt"]
    (folder / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    one = commands.add_parser("bundle")
    one.add_argument("--target", required=True)
    one.add_argument("--binary", required=True)
    one.add_argument("--version", required=True)
    one.add_argument("--out", required=True)
    two = commands.add_parser("manifest")
    two.add_argument("--dir", required=True)
    two.add_argument("--tag", required=True)
    args = parser.parse_args(argv)
    if args.command == "bundle":
        print(bundle(args.target, args.binary, args.version, args.out))
    else:
        print("\n".join(manifest(args.dir, args.tag)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
