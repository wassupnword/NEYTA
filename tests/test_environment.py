"""Guards against the breakage described in build plan section 9.

Renaming the project folder ("untitled folder 2" -> "NEYTA") left every venv
and every symlink pointing at a path that no longer existed. A broken pip
fails in a way that looks like a stale package index, which is how yt-dlp came
to be diagnosed as "not version rot" when it was exactly that.

These assertions are cheap and they make that class of failure loud. They skip
rather than fail on a checkout where the venvs have not been built yet, so a
clean clone still runs green.
"""

from __future__ import annotations

import subprocess

import pytest

from neyta import config

pytestmark = pytest.mark.env

uvr_venv_missing = pytest.mark.skipif(
    not config.UVR_PYTHON.exists(),
    reason="uvr-local/.venv not built — run tools/setup.sh",
)


class TestNoDanglingSymlinks:
    @uvr_venv_missing
    def test_no_symlink_in_this_checkout_points_outside_it(self):
        """The precise failure from section 9: absolute paths baked into a
        venv survive a folder rename and stop resolving."""
        dangling = []
        for path in config.REPO_ROOT.rglob("*"):
            if ".git" in path.parts:
                continue
            if path.is_symlink() and not path.exists():
                dangling.append(f"{path} -> {path.readlink()}")
        assert not dangling, "dangling symlinks:\n  " + "\n  ".join(dangling)


class TestInterpreters:
    @uvr_venv_missing
    def test_uvr_python_is_311(self):
        out = subprocess.run(
            [str(config.UVR_PYTHON), "-V"], capture_output=True, text=True, timeout=30
        )
        assert out.returncode == 0, out.stderr
        assert out.stdout.startswith("Python 3.11"), out.stdout

    @uvr_venv_missing
    def test_uvr_venv_can_report_what_is_installed(self):
        """A venv that cannot enumerate its own packages gives confidently
        wrong answers about what is installed — the bug that cost the last
        session an afternoon and produced the false "not version rot" call.

        Checked via importlib.metadata rather than pip: uv builds venvs without
        pip, so a missing pip here is normal and proves nothing either way.
        """
        out = subprocess.run(
            [str(config.UVR_PYTHON), "-c",
             "import importlib.metadata as m; print(m.version('audio-separator'))"],
            capture_output=True, text=True, timeout=60,
        )
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip(), "no version reported for audio-separator"


class TestFfmpeg:
    @uvr_venv_missing
    def test_an_ffmpeg_is_reachable(self):
        # There is no Homebrew on this machine, so on a built checkout the
        # only ffmpeg is the static binary symlinked into uvr-local's venv.
        # Before setup.sh has run there is legitimately none, hence the guard.
        assert config.find_ffmpeg() is not None, (
            "no system ffmpeg and no static binary in uvr-local/.venv/bin"
        )

    def test_ffmpeg_runs(self):
        ffmpeg = config.find_ffmpeg()
        if ffmpeg is None:
            pytest.skip("no ffmpeg")
        out = subprocess.run(
            [str(ffmpeg), "-version"], capture_output=True, text=True, timeout=30
        )
        assert out.returncode == 0
        assert "ffmpeg version" in out.stdout


class TestUvrLocal:
    @uvr_venv_missing
    def test_uvr_imports_and_exposes_its_presets(self):
        code = (
            "import sys; sys.path.insert(0, %r)\n"
            "from uvr import PRESETS\n"
            "print(','.join(sorted(PRESETS)))\n" % str(config.UVR_ROOT)
        )
        out = subprocess.run(
            [str(config.UVR_PYTHON), "-c", code],
            capture_output=True, text=True, timeout=120,
        )
        assert out.returncode == 0, out.stderr
        presets = set(out.stdout.strip().split(","))
        exposed = {o.preset for o in config.STEM_OPTIONS if o.preset}
        assert exposed <= presets, f"config exposes presets uvr.py lacks: {exposed - presets}"
        assert presets <= exposed, f"uvr.py has presets NEYTA hides: {presets - exposed}"

    def test_the_model_checkpoints_are_present(self):
        models = config.UVR_ROOT / "models"
        if not models.exists():
            pytest.skip("models not downloaded")
        # Plain files, unaffected by the rename. Re-downloading is pointless.
        assert (models / "model_bs_roformer_ep_317_sdr_12.9755.ckpt").exists()


class TestYtDlp:
    def test_yt_dlp_is_recent_enough(self):
        """The installed version was nine months stale, and the old release
        only worked with a player_client override that capped audio at the
        muxed 360p stream. Anything from 2026 has the default client
        selection that returns the full DASH ladder."""
        yt_dlp = pytest.importorskip("yt_dlp")
        year = int(yt_dlp.version.__version__.split(".")[0])
        assert year >= 2026, f"yt-dlp {yt_dlp.version.__version__} is too old"
