#!/usr/bin/env bash
# Fresh-install bootstrap for uvr.py.
#
# Self-contained and namespaced: everything lands inside this folder
# (uvr-local/), nothing is installed system-wide, no sudo, no Homebrew.
#   uvr-local/bin/       uv + uvx
#   uvr-local/python/    standalone CPython 3.11 (uv-managed)
#   uvr-local/.venv/     the virtualenv, incl. an ffmpeg shim
#   uvr-local/models/    UVR model checkpoints (downloaded on first use)
#
# Safe to re-run; it skips whatever is already in place.
#
#   ./setup.sh            install everything
#   ./setup.sh --check    verify an existing install, install nothing

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export UV_PYTHON_INSTALL_DIR="$HERE/python"
UV="$HERE/bin/uv"
VENV="$HERE/.venv"
PY="$VENV/bin/python"
PYTHON_VERSION="3.11"

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

say()  { printf '\033[1m[uvr-setup]\033[0m %s\n' "$*"; }
fail() { printf '\033[31m[uvr-setup] %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- checks
if [ "$CHECK_ONLY" = 1 ]; then
    [ -x "$PY" ] || fail "no venv at $VENV — run ./setup.sh first"
    say "python:         $("$PY" -V)"
    "$PY" - "$HERE" <<'EOF' || exit 1
import importlib.metadata as md, subprocess, sys
sys.path.insert(0, sys.argv[1])
try:
    print(f"[uvr-setup] audio-separator: {md.version('audio-separator')}")
except md.PackageNotFoundError:
    sys.exit("[uvr-setup] audio-separator MISSING — re-run ./setup.sh")

# Resolve ffmpeg exactly the way uvr.py does at runtime, then actually run it.
# A bare `which ffmpeg` here would say MISSING even on a good install, since
# this shell doesn't have the venv's bin dir on PATH.
import uvr
try:
    exe = uvr.ensure_ffmpeg()
    ver = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=30)
    assert ver.returncode == 0, ver.stderr[:200]
except Exception as e:
    sys.exit(f"[uvr-setup] ffmpeg BROKEN: {e}")
print(f"[uvr-setup] ffmpeg:         {ver.stdout.splitlines()[0].split(' Copyright')[0]}")
print(f"[uvr-setup] presets:        {', '.join(uvr.PRESETS)}")
EOF
    say "OK — ready to go."
    exit 0
fi

# ---------------------------------------------------------------- uv
if [ ! -x "$UV" ]; then
    say "installing uv into $HERE/bin ..."
    curl -LsSf https://astral.sh/uv/install.sh \
        | UV_INSTALL_DIR="$HERE/bin" UV_NO_MODIFY_PATH=1 sh >/dev/null \
        || fail "could not download uv (network?)"
fi
say "uv:  $("$UV" --version)"

# ---------------------------------------------------------------- python + venv
# The system python on macOS is 3.9; audio-separator needs >= 3.10, so uv
# fetches a standalone CPython into this folder rather than touching the OS.
if [ ! -x "$PY" ]; then
    say "creating Python $PYTHON_VERSION venv ..."
    "$UV" venv --python "$PYTHON_VERSION" "$VENV" >/dev/null
fi
say "python: $("$PY" -V)"

# ---------------------------------------------------------------- deps
say "installing dependencies (first run pulls ~1GB of torch/onnx, be patient) ..."
DEPS="$HERE/requirements.lock.txt"
[ -f "$DEPS" ] || DEPS="$HERE/requirements.txt"
"$UV" pip install --python "$PY" -r "$DEPS"

# ---------------------------------------------------------------- ffmpeg
# audio-separator shells out to ffmpeg. No Homebrew here, so use the
# arm64 binary that ships inside the imageio-ffmpeg wheel and link it onto
# the venv's PATH — unless a real system ffmpeg already exists.
if [ ! -e "$VENV/bin/ffmpeg" ] && ! command -v ffmpeg >/dev/null 2>&1; then
    say "linking bundled ffmpeg into the venv ..."
    FFMPEG_BIN="$("$PY" -c 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())')"
    [ -x "$FFMPEG_BIN" ] || fail "bundled ffmpeg not found"
    ln -sf "$FFMPEG_BIN" "$VENV/bin/ffmpeg"
fi

mkdir -p "$HERE/models"

say "verifying ..."
exec "$0" --check
