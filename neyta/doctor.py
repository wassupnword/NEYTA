"""`python -m neyta doctor` — environment self-check.

Exists because of build plan section 9. The last time this project's
environment was silently broken, a tool reported a confidently wrong answer
and the wrong conclusion was written down as fact. This prints what is
actually true, in one screen, before anyone reasons from memory.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import config

OK = "ok"
WARN = "warn"
FAIL = "fail"

_MARK = {OK: "✓", WARN: "!", FAIL: "✗"}


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    fix: str = ""


def _run(args: list[str], timeout: float = 60) -> tuple[int, str, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)


def check_python() -> Check:
    major, minor = sys.version_info[:2]
    if (major, minor) < (3, 11):
        return Check(
            "python", FAIL, f"{sys.version.split()[0]}",
            "NEYTA needs 3.11+. System Python is 3.9.6 and too old; "
            "use uvr-local's standalone CPython via tools/setup.sh.",
        )
    return Check("python", OK, sys.version.split()[0])


def check_dangling_symlinks() -> Check:
    dangling = []
    for path in config.REPO_ROOT.rglob("*"):
        if ".git" in path.parts:
            continue
        if path.is_symlink() and not path.exists():
            dangling.append(path)
    if dangling:
        listing = "\n      ".join(
            f"{p.relative_to(config.REPO_ROOT)} -> {p.readlink()}" for p in dangling[:8]
        )
        return Check(
            "symlinks", FAIL, f"{len(dangling)} dangling\n      {listing}",
            "Venvs and symlinks store absolute paths and do not survive a "
            "folder rename. Rebuild with tools/setup.sh.",
        )
    return Check("symlinks", OK, "none dangling")


def check_ffmpeg() -> Check:
    ffmpeg = config.find_ffmpeg()
    if ffmpeg is None:
        return Check(
            "ffmpeg", FAIL, "not found",
            "Expected the static arm64 binary in uvr-local/.venv/bin/ffmpeg. "
            "Rebuild uvr-local with tools/setup.sh.",
        )
    code, out, _ = _run([str(ffmpeg), "-version"], timeout=30)
    if code != 0:
        return Check("ffmpeg", FAIL, f"{ffmpeg} will not run")
    version = out.splitlines()[0].replace("ffmpeg version ", "").split()[0]
    source = "system" if str(ffmpeg).startswith("/usr") else "uvr-local (static)"
    return Check("ffmpeg", OK, f"{version} — {source}")


def check_yt_dlp() -> Check:
    try:
        import yt_dlp
    except ImportError:
        return Check("yt-dlp", FAIL, "not installed", "tools/setup.sh")
    version = yt_dlp.version.__version__
    year = int(version.split(".")[0])
    if year < 2026:
        return Check(
            "yt-dlp", FAIL, f"{version} — stale",
            "Releases older than 2026 need a player_client=android override "
            "that caps you at the muxed 360p stream with no audio-only "
            "formats. Upgrade before concluding anything about extraction.",
        )
    return Check("yt-dlp", OK, version)


def check_uvr() -> Check:
    if not config.UVR_PYTHON.exists():
        return Check("uvr-local", FAIL, "venv missing", "tools/setup.sh")
    code, out, err = _run(
        [str(config.UVR_PYTHON), "-c",
         f"import sys; sys.path.insert(0, {str(config.UVR_ROOT)!r});"
         "from uvr import PRESETS; print(len(PRESETS))"],
        timeout=120,
    )
    if code != 0:
        return Check("uvr-local", FAIL, err.splitlines()[-1] if err else "import failed")
    return Check("uvr-local", OK, f"{out} presets")


def check_models() -> Check:
    models = config.UVR_ROOT / "models"
    if not models.exists():
        return Check("uvr models", WARN, "not downloaded",
                     "They fetch on first separation.")
    size = sum(f.stat().st_size for f in models.rglob("*") if f.is_file())
    key = models / "model_bs_roformer_ep_317_sdr_12.9755.ckpt"
    status = OK if key.exists() else WARN
    return Check("uvr models", status, f"{size / 1e9:.1f} GB in {models.name}/")


def check_qt() -> Check:
    try:
        import PySide6
        from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: F401
    except ImportError as exc:
        return Check("PySide6", FAIL, str(exc), "tools/setup.sh")
    return Check("PySide6", OK, f"{PySide6.__version__} with QtWebEngine")


def check_keychain() -> Check:
    try:
        import keyring
    except ImportError:
        return Check("keychain", FAIL, "keyring not installed")
    backend = keyring.get_keyring().__class__.__name__
    if "fail" in backend.lower():
        return Check("keychain", FAIL, backend, "No usable keyring backend.")
    return Check("keychain", OK, backend)


def check_slskd() -> Check:
    paths = config.Paths.default()
    binary = paths.slskd_dir / "slskd"
    if binary.exists():
        return Check("slskd", OK, str(binary))
    return Check(
        "slskd", WARN, "not bootstrapped",
        "Downloaded on first use of the Soulseek tab. The YouTube and "
        "SoundCloud tabs do not need it.",
    )


def check_samplette() -> Check:
    if not config.SAMPLETTE_DB.exists():
        return Check(
            "samplette", WARN, "no library",
            "Run samplette-local once to build one. Shuffle is unavailable "
            "until then; the other tabs do not need it.",
        )
    try:
        from .core.samplette import SampletteLibrary

        with SampletteLibrary() as lib:
            s = lib.stats()
    except Exception as exc:  # noqa: BLE001
        return Check("samplette", FAIL, str(exc)[:80])

    size = config.SAMPLETTE_DB.stat().st_size / 1e6
    detail = f"{s.ready:,} playable of {s.total:,} ({size:.0f} MB)"
    if s.ready == 0:
        return Check("samplette", WARN, detail,
                     "Nothing resolved to a video yet — let samplette-local crawl.")
    return Check("samplette", OK, detail)


CHECKS = (
    check_python, check_dangling_symlinks, check_ffmpeg, check_yt_dlp,
    check_qt, check_keychain, check_uvr, check_models, check_samplette,
    check_slskd,
)


def run_all() -> list[Check]:
    return [c() for c in CHECKS]


def report(checks: list[Check] | None = None, stream=sys.stdout) -> int:
    checks = run_all() if checks is None else checks
    width = max(len(c.name) for c in checks)

    print(f"NEYTA environment — {config.REPO_ROOT}", file=stream)
    print("-" * 60, file=stream)
    for c in checks:
        print(f"  {_MARK[c.status]} {c.name:<{width}}  {c.detail}", file=stream)
    print("-" * 60, file=stream)

    failed = [c for c in checks if c.status == FAIL]
    if failed:
        print("\nProblems:", file=stream)
        for c in failed:
            print(f"  {c.name}: {c.fix or c.detail}", file=stream)
    else:
        print("All required components present.", file=stream)
    return 1 if failed else 0
