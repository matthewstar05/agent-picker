"""Put a Herdr binary in this checkout's bin/ folder, where agent looks for it.

    python scripts/herdr_bin.py build     compile herdr/ with cargo (your own changes)
    python scripts/herdr_bin.py fetch     download the latest release's build
    python scripts/herdr_bin.py fetch --tag v2.0.0
    python scripts/herdr_bin.py status    which binary is in place

build needs the Rust toolchain named in herdr/rust-toolchain.toml, Zig 0.16,
cmake and ninja; herdr/CONTRIBUTING.md has the details. fetch needs nothing but
a network connection, and checks the download against the release's checksums.
bin/ is ignored by Git, so neither touches the repository.
"""

import argparse
import hashlib
import http.client
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "matthewstar05/agent-picker"
BIN = ROOT / "bin"


def target(system=None, machine=None):
    """This machine's release target name, such as macos-aarch64."""
    system, machine = system or sys.platform, (machine or platform.machine()).lower()
    arch = {"arm64": "aarch64", "aarch64": "aarch64", "x86_64": "x86_64", "amd64": "x86_64"}.get(machine)
    if system == "darwin" and arch:
        return f"macos-{arch}"
    if system == "win32" and arch == "x86_64":
        return "windows-x86_64"
    raise SystemExit(f"herdr_bin: no release build for {system} {machine}; use build")


def binary():
    return BIN / ("herdr.exe" if sys.platform == "win32" else "herdr")


def place(source):
    """Install a built or downloaded file as bin/herdr without disturbing a
    running copy: the old file is replaced in one step."""
    BIN.mkdir(exist_ok=True)
    staged = BIN / (binary().name + ".new")
    shutil.copyfile(source, staged)
    staged.chmod(0o755)
    os.replace(staged, binary())
    return binary()


def build(profile="release"):
    cargo = shutil.which("cargo")
    if not cargo:
        raise SystemExit("herdr_bin: cargo was not found. Install Rust with rustup (https://rustup.rs), "
                         "plus Zig 0.16, cmake and ninja, or use: python scripts/herdr_bin.py fetch")
    command = [cargo, "build", "--locked"] + (["--release"] if profile == "release" else [])
    subprocess.run(command, cwd=ROOT / "herdr", check=True)
    built = ROOT / "herdr" / "target" / profile / binary().name
    return place(built)


def download(url, tries=4, wait=time.sleep):
    """The bytes at url. A dropped connection is retried before giving up."""
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=120) as response:
                return response.read()
        except (OSError, http.client.HTTPException) as error:
            if attempt == tries - 1:
                raise SystemExit(f"herdr_bin: could not download {url}: {error}")
            wait(2 * (attempt + 1))


def fetch(tag=None, get=download):
    name = f"herdr-{target()}" + (".zip" if sys.platform == "win32" else "")
    base = (f"https://github.com/{REPOSITORY}/releases/download/{tag}" if tag
            else f"https://github.com/{REPOSITORY}/releases/latest/download")
    sums = get(f"{base}/SHA256SUMS.txt").decode("utf-8")
    want = next((line.split()[0] for line in sums.splitlines()
                 if line.split()[1:] and line.split()[1].lstrip("*") == name), None)
    if not want:
        raise SystemExit(f"herdr_bin: that release has no {name}")
    data = get(f"{base}/{name}")
    if hashlib.sha256(data).hexdigest() != want:
        raise SystemExit("herdr_bin: the download is damaged (checksum mismatch); try again")
    BIN.mkdir(exist_ok=True)
    packed = BIN / (name + ".download")
    packed.write_bytes(data)
    try:
        if name.endswith(".zip"):
            with zipfile.ZipFile(packed) as archive:
                archive.extractall(BIN)     # herdr.exe and its ConPTY files
            return binary()
        return place(packed)
    finally:
        packed.unlink()


def status():
    path = binary()
    if not path.is_file():
        return f"No Herdr in {BIN}; agent uses the herdr on PATH."
    try:
        version = subprocess.run([str(path), "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        version = "does not run"
    return f"{path}: {version}"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("build")
    make.add_argument("--debug", action="store_true", help="a faster, unoptimized build")
    get = commands.add_parser("fetch")
    get.add_argument("--tag", help="a release tag such as v2.0.0; the latest release by default")
    commands.add_parser("status")
    args = parser.parse_args(argv)
    if args.command == "build":
        print(build("debug" if args.debug else "release"))
    elif args.command == "fetch":
        print(fetch(args.tag))
    print(status())
    return 0


if __name__ == "__main__":
    sys.exit(main())
