"""First run, the .app bundle, and the cold-start rehearsal.

The rehearsal is the one that matters: it proves the package imports and
behaves from a directory that has none of this machine's state — no
preferences, no Keychain entries, no caches, no downloads.
"""

from __future__ import annotations

import importlib.util
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from neyta import config

ROOT = Path(__file__).resolve().parent.parent
MAKE_APP = ROOT / "tools" / "make_app.py"


def load_make_app():
    if not MAKE_APP.exists():
        pytest.skip("tools/make_app.py is missing")
    spec = importlib.util.spec_from_file_location("_make_app", MAKE_APP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Cold start
# ---------------------------------------------------------------------------


class TestColdStart:
    """A machine that has never run NEYTA."""

    def test_paths_are_created_from_nothing(self, tmp_path):
        paths = config.Paths.under(tmp_path / "fresh")
        assert not paths.support.exists()
        paths.ensure()
        for directory in (paths.support, paths.cache, paths.downloads,
                          paths.logs, paths.preview_dir, paths.clips_dir):
            assert directory.is_dir()

    def test_settings_read_their_defaults_with_no_stored_state(self, tmp_path):
        from neyta.settings import FakeKeyring, MemoryPrefs, SecretStore, Settings

        settings = Settings(
            paths=config.Paths.under(tmp_path).ensure(),
            prefs=MemoryPrefs(), secrets=SecretStore(backend=FakeKeyring()),
        )
        assert settings.stem_selection == list(config.DEFAULT_STEMS)
        assert settings.format_for("youtube").key == config.WAV_48_24.key
        assert not settings.onboarding_complete

    def test_the_cache_builds_itself(self, tmp_path):
        from neyta.core.cache import Cache

        db = config.Paths.under(tmp_path).cache_db
        with Cache(db) as cache:
            cache.put_search("youtube", "q", 5, ["x"])
        assert db.exists()

    def test_only_soulseek_is_missing_on_a_fresh_install(self, tmp_path):
        from neyta.settings import FakeKeyring, MemoryPrefs, SecretStore, Settings

        settings = Settings(
            paths=config.Paths.under(tmp_path).ensure(),
            prefs=MemoryPrefs(), secrets=SecretStore(backend=FakeKeyring()),
        )
        assert [s.key for s in settings.missing_required()] == ["soulseek"]

    def test_the_engine_needs_no_credentials(self):
        from neyta.core.engine import Engine

        opts = Engine().base_opts()
        assert "cookiefile" not in opts
        assert "cookiesfrombrowser" not in opts

    def test_every_module_imports_without_side_effects(self):
        """Nothing may write to the user's real folders at import time."""
        modules = [
            "neyta.config", "neyta.settings", "neyta.app", "neyta.cli",
            "neyta.doctor", "neyta.core.engine", "neyta.core.convert",
            "neyta.core.captions", "neyta.core.phrase", "neyta.core.stems",
            "neyta.core.samplette", "neyta.core.jobs", "neyta.core.cache",
            "neyta.core.naming", "neyta.providers.base",
            "neyta.providers.youtube", "neyta.providers.soundcloud",
            "neyta.providers.bandcamp", "neyta.providers.soulseek",
            "neyta.vendor.slskd_bootstrap",
        ]
        code = "import " + ", ".join(modules)
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True,
            timeout=120, cwd=str(ROOT),
        )
        assert result.returncode == 0, result.stderr

    def test_the_cli_runs_from_a_clean_environment(self, tmp_path):
        result = subprocess.run(
            [sys.executable, "-m", "neyta", "--help"],
            capture_output=True, text=True, timeout=120, cwd=str(ROOT),
            env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        )
        assert result.returncode == 0
        assert "doctor" in result.stdout

    def test_doctor_reports_rather_than_crashing_on_a_bare_machine(self, tmp_path):
        result = subprocess.run(
            [sys.executable, "-m", "neyta", "doctor"],
            capture_output=True, text=True, timeout=300, cwd=str(ROOT),
            env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        )
        # Exit 1 is legitimate — it means something is genuinely missing.
        assert result.returncode in (0, 1)
        assert "NEYTA environment" in result.stdout


# ---------------------------------------------------------------------------
# Onboarding
# ---------------------------------------------------------------------------


class TestReadiness:
    @pytest.fixture
    def settings(self, tmp_path):
        from neyta.settings import FakeKeyring, MemoryPrefs, SecretStore, Settings

        return Settings(
            paths=config.Paths.under(tmp_path).ensure(),
            prefs=MemoryPrefs(), secrets=SecretStore(backend=FakeKeyring()),
        )

    def test_the_three_open_tabs_are_ready_with_no_setup(self, settings):
        from neyta.ui.onboarding import Readiness

        rows = {label: ready for label, ready, _ in Readiness(settings).rows()}
        assert rows["YouTube"] and rows["SoundCloud"] and rows["Bandcamp"]

    def test_soulseek_is_reported_as_needing_setup(self, settings, tmp_path):
        from neyta.ui.onboarding import Readiness
        from neyta.vendor.slskd_bootstrap import SlskdBootstrap

        rows = dict(
            (label, detail)
            for label, _, detail in Readiness(
                settings, SlskdBootstrap(config.Paths.under(tmp_path))
            ).rows()
        )
        assert "Settings" in rows["Soulseek"]

    def test_spotify_is_reported_as_needing_its_checkout(self, settings, tmp_path):
        from neyta.ui.onboarding import Readiness
        from neyta.vendor.lucida_bootstrap import LucidaBootstrap

        rows = dict(
            (label, detail) for label, _, detail in Readiness(
                settings, lucida=LucidaBootstrap(root=tmp_path / "not here")
            ).rows()
        )
        assert "lucida-flow" in rows["Spotify"]

    def test_the_stem_row_names_the_engine_actually_in_force(self, settings):
        # A machine with no uvr-local and a licence key can separate fine;
        # reporting "not built" there would be wrong.
        from neyta.ui.onboarding import Readiness

        settings.stem_engine = "lalal"
        settings.set_credential("lalal", "api_key", "licence")
        rows = Readiness(settings).rows()
        assert any("LALAL.AI" in label and ready for label, ready, _ in rows)

    def test_readiness_survives_a_machine_with_nothing_built(self, settings):
        from neyta.ui.onboarding import Readiness

        # It must describe an unbuilt machine, not raise on one.
        assert Readiness(settings).rows()


class TestWelcome:
    def test_it_is_shown_only_on_the_first_run(self, tmp_path):
        from neyta.settings import FakeKeyring, MemoryPrefs, SecretStore, Settings
        from neyta.ui.onboarding import WelcomeDialog

        settings = Settings(
            paths=config.Paths.under(tmp_path).ensure(),
            prefs=MemoryPrefs(), secrets=SecretStore(backend=FakeKeyring()),
        )
        assert WelcomeDialog.should_show(settings)
        settings.onboarding_complete = True
        assert not WelcomeDialog.should_show(settings)


# ---------------------------------------------------------------------------
# The app bundle
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def module():
    return load_make_app()


class TestAppBundle:
    def test_the_launcher_is_valid_shell(self, module, tmp_path):
        script = module.render_launcher(tmp_path)
        (tmp_path / "launcher.sh").write_text(script)
        result = subprocess.run(
            ["bash", "-n", str(tmp_path / "launcher.sh")], capture_output=True
        )
        assert result.returncode == 0, result.stderr

    def test_the_repository_bundle_locates_its_checkout(self, module):
        script = module.render_launcher()
        assert 'dirname "$0"' in script
        assert str(ROOT) not in script

    def test_a_missing_environment_produces_an_alert_not_a_silent_failure(
        self, module, tmp_path
    ):
        script = module.render_launcher(Path("/nonexistent/moved"))
        script = script.replace("/usr/bin/osascript -e", "echo ALERT:")
        launcher = tmp_path / "moved.sh"
        launcher.write_text(script)
        launcher.chmod(0o755)

        result = subprocess.run(
            ["bash", str(launcher)], capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 1
        assert "ALERT" in result.stdout
        assert "setup.sh" in result.stdout

    def test_building_produces_a_launchable_bundle(self, module, tmp_path):
        bundle = module.build(tmp_path)

        assert bundle.name == "NEYTA.app"
        executable = bundle / "Contents" / "MacOS" / "NEYTA"
        assert executable.exists()
        assert executable.stat().st_mode & 0o111, "not executable"

        info = plistlib.loads(
            (bundle / "Contents" / "Info.plist").read_bytes()
        )
        assert info["CFBundleExecutable"] == "NEYTA"
        assert info["CFBundleIdentifier"] == "com.neyta.app"
        assert info["CFBundlePackageType"] == "APPL"
        assert info["NSHighResolutionCapable"] is True

    def test_rebuilding_replaces_rather_than_accumulates(self, module, tmp_path):
        module.build(tmp_path)
        bundle = module.build(tmp_path)
        assert len(list(tmp_path.glob("*.app"))) == 1
        assert bundle.exists()


# ---------------------------------------------------------------------------
# Documentation
# ---------------------------------------------------------------------------


class TestDocs:
    def test_the_readme_screenshots_exist(self):
        readme = (ROOT / "README.md").read_text("utf-8")
        import re

        for image in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", readme):
            assert (ROOT / image).exists(), f"README references missing {image}"

    def test_the_readme_states_the_real_ceilings(self):
        readme = (ROOT / "README.md").read_text("utf-8")
        assert "129k" in readme and "160k" in readme
        assert "no 320 on YouTube" in readme

    def test_the_readme_says_slskd_is_not_the_soulseek_app(self):
        assert "not the Soulseek app" in (ROOT / "README.md").read_text("utf-8")

    def test_setup_is_executable(self):
        setup = ROOT / "tools" / "setup.sh"
        assert setup.exists() and setup.stat().st_mode & 0o111
