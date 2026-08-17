#!/usr/bin/env bash
#
# Builds every environment NEYTA needs, from scratch, with no admin password
# and no Homebrew.
#
# Safe to re-run. Run it after moving or renaming the project folder: venvs
# store absolute paths, so a rename leaves every interpreter and every symlink
# pointing at somewhere that no longer exists (build plan section 9). That
# failure is silent and it produces confidently wrong answers about what is
# installed.
#
#   ./tools/setup.sh          rebuild what is missing or broken
#   ./tools/setup.sh --force  rebuild everything unconditionally
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

UVR_SETUP="$ROOT/uvr-local/setup.sh"
UV="$ROOT/uvr-local/bin/uv"
NEYTA_VENV="$ROOT/.venv-neyta"
UVR_VENV="$ROOT/uvr-local/.venv"

FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m  ! %s\033[0m\n' "$*"; }
die()  { printf '\033[31m  ✗ %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
say "Building uvr-local/.venv (audio-separator, torch, onnxruntime)"

[[ -x "$UVR_SETUP" ]] || die "missing executable $UVR_SETUP"
if [[ $FORCE -eq 1 ]]; then
    rm -rf "$UVR_VENV"
fi
"$UVR_SETUP"
[[ -x "$UV" ]] || die "uv bootstrap failed at $UV"
PY311="$UVR_VENV/bin/python"
echo "  uv $("$UV" --version | awk '{print $2}')"

# ---------------------------------------------------------------------------
say "Building .venv-neyta (PySide6, yt-dlp, keyring)"

if [[ $FORCE -eq 1 || ! -x "$NEYTA_VENV/bin/python" ]]; then
    rm -rf "$NEYTA_VENV"
    "$UV" venv --python "$PY311" "$NEYTA_VENV" >/dev/null
    if [[ -f "$ROOT/requirements.lock.txt" ]]; then
        "$UV" pip install --python "$NEYTA_VENV/bin/python" \
            -r "$ROOT/requirements.lock.txt" 2>&1 | tail -3
    else
        "$UV" pip install --python "$NEYTA_VENV/bin/python" \
            -e "$ROOT[dev]" 2>&1 | tail -3
    fi
else
    echo "  present — pass --force to rebuild"
fi

# Always install NEYTA itself, editable. The lockfile pins dependencies but
# does not contain the package, and without this `python -m neyta` only works
# from the project directory.
"$UV" pip install --python "$NEYTA_VENV/bin/python" --no-deps -e "$ROOT" \
    2>&1 | tail -1

# ---------------------------------------------------------------------------
say "Verifying"

"$NEYTA_VENV/bin/python" -m neyta doctor || warn "doctor reported problems"

echo
say "Done. Next:"
echo "  $NEYTA_VENV/bin/python -m pytest        the unit suite"
echo "  $NEYTA_VENV/bin/python -m neyta doctor  environment self-check"
