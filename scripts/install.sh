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

# Everything runs from main, so a download cut off partway runs nothing.
main() {
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

  [ ! -e "$dest/.git" ] || fail "$dest is a Git checkout; update it with: agent --picker-update"

  sha256() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
    else shasum -a 256 "$1" | cut -d' ' -f1; fi
  }

  tmp=$(mktemp -d "${TMPDIR:-/tmp}/agent-picker.XXXXXX")
  trap 'rm -rf "$tmp"' EXIT

  # Python 3.9 or later runs agent-picker. A new Mac has none: /usr/bin/python3
  # is only a stub until the Command Line Tools are installed, and running it
  # opens an install dialog. Then this downloads a private Python (a pinned
  # python-build-standalone build, checked against its hash) into
  # ~/.local/share/agent-picker-python, outside the app folder so updates keep it.
  pyhome=${AGENT_PICKER_PYTHON_HOME:-$(dirname "$dest")/agent-picker-python}
  pbs=20261026
  pbs_version=3.12.16
  case "$arch" in
    aarch64) pbs_sum=353d5f1b4fd2715e7053f12f068435d5bbad16b91e057006b5d01e0925709a7e ;;
    x86_64) pbs_sum=37c38fda76fec1fbf310adcbafb7f0d96af13b5511bf8ea954c3aa8eec185734 ;;
  esac

  usable() {
    [ -x "$1" ] || return 1
    if [ "$1" = /usr/bin/python3 ]; then
      devdir=$(xcode-select -p 2>/dev/null) || return 1
      [ -d "$devdir" ] || return 1
    fi
    "$1" -c 'import sys; raise SystemExit(sys.version_info < (3, 9))' >/dev/null 2>&1
  }

  private_python() {
    name="cpython-$pbs_version+$pbs-$arch-apple-darwin-install_only_stripped.tar.gz"
    url="https://github.com/astral-sh/python-build-standalone/releases/download/$pbs/$(printf %s "$name" | sed 's/+/%2B/')"
    say "No Python 3.9 or later found; downloading a private Python $pbs_version for agent-picker..."
    curl -fsSL --retry 3 "$url" -o "$tmp/python.tar.gz" || fail "could not download Python from $url"
    [ "$(sha256 "$tmp/python.tar.gz")" = "$pbs_sum" ] || fail "the Python download is damaged (checksum mismatch); try again"
    mkdir -p "$tmp/python"
    tar -xzf "$tmp/python.tar.gz" -C "$tmp/python"
    [ -x "$tmp/python/python/bin/python3" ] || fail "the Python download is incomplete"
    mkdir -p "$(dirname "$pyhome")"
    rm -rf "$pyhome"
    mv "$tmp/python/python" "$pyhome"
  }

  python=${AGENT_PICKER_PYTHON:-}
  if [ -n "$python" ]; then
    usable "$python" || fail "AGENT_PICKER_PYTHON=$python is not Python 3.9 or later"
  elif [ -z "${AGENT_PICKER_PRIVATE_PYTHON:-}" ] && usable "$pyhome/bin/python3"; then
    python=$pyhome/bin/python3
  else
    for candidate in python3 python; do
      [ -z "${AGENT_PICKER_PRIVATE_PYTHON:-}" ] || break
      found=$(command -v "$candidate" 2>/dev/null) || continue
      if usable "$found"; then python=$found; break; fi
    done
    if [ -z "$python" ]; then
      private_python
      python=$pyhome/bin/python3
      usable "$python" || fail "the downloaded Python does not run on this Mac"
    fi
  fi

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

  "$python" "$dest/scripts/bootstrap.py" "$@" </dev/null
  say ""
  say "Done. In a new terminal, run: agent"

  # Open the workspace now when this ran at a terminal: curl ... | sh leaves
  # stdin on the pipe, so agent reads the terminal itself. Not inside Herdr, and
  # not when an install updates itself (agent --picker-update sets
  # AGENT_PICKER_HOME) or AGENT_PICKER_NO_OPEN=1.
  agent=$HOME/.local/bin/agent
  if [ -z "${AGENT_PICKER_NO_OPEN:-}" ] && [ -z "${AGENT_PICKER_HOME:-}" ] && [ "${HERDR_ENV:-}" != 1 ] &&
     [ -x "$agent" ] && [ -t 1 ] && (: </dev/tty) 2>/dev/null; then
    say "Opening agent..."
    # Seven panes need room: zoom a small Terminal.app window to fill the screen.
    if [ "${TERM_PROGRAM:-}" = Apple_Terminal ]; then
      cols=$(stty size </dev/tty 2>/dev/null | cut -d' ' -f2)
      if [ "${cols:-0}" -lt 160 ]; then printf '\033[9;1t' >/dev/tty; sleep 0.5; fi
    fi
    rm -rf "$tmp"
    trap - EXIT
    PATH=$HOME/.local/bin:$PATH
    export PATH
    cd "$HOME"
    exec "$agent" </dev/tty
  fi
}

main "$@"
