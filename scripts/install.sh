#!/bin/sh
# Installs agent-picker together with its own Herdr:
#
#   curl -fsSL https://github.com/matthewstar05/agent-picker/releases/latest/download/install.sh | sh
#
# The release bundle for this Mac goes into ~/.local/share/agent-picker
# (AGENT_PICKER_HOME changes that), its commands into ~/.local/bin. A Herdr you
# already have is left alone. Running this again updates the install.
# Arguments after `sh -s --` go to the component installer, for example
#   curl -fsSL .../install.sh | sh -s -- --components usage,fs,herdr
set -eu

repo=matthewstar05/agent-picker
releases=${AGENT_PICKER_RELEASES_URL:-https://github.com/$repo/releases/latest/download}
dest=${AGENT_PICKER_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/agent-picker}

say() { printf '%s\n' "$*"; }
fail() { say "agent-picker: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || fail "needs $1"; }
need curl
need tar

case "$(uname -s)" in
  Darwin) os=macos ;;
  MINGW* | MSYS* | CYGWIN*) fail "on Windows, run this in PowerShell: irm $releases/install.ps1 | iex" ;;
  *) fail "no build for $(uname -s) yet; agent-picker supports macOS and Windows" ;;
esac
case "$(uname -m)" in
  arm64 | aarch64) arch=aarch64 ;;
  x86_64 | amd64)
    # Rosetta reports x86_64 on Apple silicon.
    if [ "$(sysctl -n hw.optional.arm64 2>/dev/null || true)" = 1 ]; then arch=aarch64; else arch=x86_64; fi ;;
  *) fail "no build for $(uname -m)" ;;
esac
target="$os-$arch"

python=${AGENT_PICKER_PYTHON:-}
if [ -z "$python" ]; then
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 9))' 2>/dev/null; then
      python=$(command -v "$candidate"); break
    fi
  done
fi
[ -n "$python" ] || fail "needs Python 3.9 or later (xcode-select --install provides one)"

[ ! -e "$dest/.git" ] || fail "$dest is a Git checkout; update it with: agent --picker-update"

sha256() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
name="agent-picker-$target.tar.gz"
say "Downloading agent-picker for $target..."
curl -fsSL "$releases/SHA256SUMS.txt" -o "$tmp/sums" || fail "cannot reach $releases"
want=$(awk -v n="$name" '$2 == n || $2 == "*" n { print $1 }' "$tmp/sums")
[ -n "$want" ] || fail "this release has no build for $target"
curl -fsSL "$releases/$name" -o "$tmp/$name" || fail "download failed"
[ "$(sha256 "$tmp/$name")" = "$want" ] || fail "download is damaged (checksum mismatch); try again"

mkdir -p "$(dirname "$dest")"
new="$dest.new.$$"
rm -rf "$new"
mkdir -p "$new"
tar -xzf "$tmp/$name" -C "$new"
[ -f "$new/scripts/bootstrap.py" ] || { rm -rf "$new"; fail "the bundle is incomplete"; }
if [ -e "$dest" ]; then
  old="$dest.old.$$"
  mv "$dest" "$old"
  mv "$new" "$dest"
  rm -rf "$old"
else
  mv "$new" "$dest"
fi

"$python" "$dest/scripts/bootstrap.py" "$@"
say ""
say "Done. Open a new terminal, then run: agent"
