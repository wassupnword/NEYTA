"""The Soulseek tab and the slskd bootstrap.

The provider is exercised against the stub slskd server that already lives in
soulseek/tests — the plan's own suggestion, and it means these run offline and
in milliseconds. Nothing here downloads slskd or touches the network.
"""

from __future__ import annotations

import importlib.util
import sys
import zipfile
from pathlib import Path

import pytest

from neyta import config
from neyta.providers import soulseek as SK
from neyta.providers.base import LocalFile, NotSupported
from neyta.vendor import slskd_bootstrap as boot


def _load_stub():
    """Borrow the stub server from soulseek's own test suite."""
    path = config.SOULSEEK_PKG_ROOT / "tests" / "test_client.py"
    if not path.exists():
        pytest.skip("soulseek/tests/test_client.py is missing")
    spec = importlib.util.spec_from_file_location("_sk_stub", path)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(config.SOULSEEK_PKG_ROOT))
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def stub():
    module = _load_stub()
    server, url = module.start_stub()
    try:
        yield module, url
    finally:
        server.shutdown()


@pytest.fixture
def provider(stub):
    module, url = stub
    from soulseek_api import SoulseekClient  # type: ignore

    client = SoulseekClient(url=url, api_key=getattr(module, "API_KEY", None),
                            username="u", password=getattr(module, "PASSWORD", "p"))
    p = SK.SoulseekProvider(client=client, search_timeout=5)
    try:
        yield p
    finally:
        p.close()


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class TestTabBasics:
    def test_it_is_known_to_the_format_matrix(self):
        assert "soulseek" in config.FORMATS
        assert config.SOURCE_CEILING_KBPS["soulseek"] is None

    def test_it_defaults_to_keeping_the_peers_file(self):
        assert config.DEFAULT_FORMAT["soulseek"] == config.ORIGINAL.key

    def test_the_ceiling_note_says_it_can_beat_the_others(self):
        assert "FLAC" in config.CEILING_NOTE["soulseek"]

    def test_whole_files_only(self):
        assert SK.SoulseekProvider().supports_spans is False

    def test_asking_for_a_span_raises(self, provider, tmp_path):
        results = provider.search("track", 5)
        with pytest.raises(NotSupported):
            provider.fetch(results[0], config.ORIGINAL, tmp_path / "x",
                           span=(1.0, 2.0))

    def test_using_it_with_no_daemon_explains_itself(self):
        bare = SK.SoulseekProvider()
        with pytest.raises(SK.SoulseekUnavailable, match="Settings"):
            bare.search("anything")

    def test_connected_is_false_without_a_client(self):
        assert SK.SoulseekProvider().connected() is False


class TestSearch:
    def test_it_returns_results(self, provider):
        assert provider.search("track", 10)

    def test_results_are_tagged_soulseek(self, provider):
        assert all(r.provider == "soulseek" for r in provider.search("track", 10))

    def test_non_audio_files_are_excluded(self, provider):
        # The stub also serves a cover.jpg.
        names = [r.title for r in provider.search("track", 10)]
        assert not any(n.endswith(".jpg") for n in names)

    def test_a_windows_path_becomes_a_readable_title(self, provider):
        # Peers share "C:\Mus\track.flac"; the tab shows "track.flac".
        titles = [r.title for r in provider.search("track", 10)]
        assert "track.flac" in titles
        assert all("\\" not in t for t in titles)

    def test_lossless_outranks_a_higher_advertised_bitrate(self, provider):
        """The stub's FLAC reports bitRate 0 and its MP3 reports 320. Ranking
        on the number alone would put the MP3 first, which is exactly the
        wrong answer on the one tab that can hand you a master."""
        results = provider.search("track", 10)
        assert results[0].title.endswith(".flac")

    def test_free_slots_rank_above_queued_ones(self):
        provider = SK.SoulseekProvider()
        free = provider.to_result(_file("a/b/c.mp3", free=True, bitrate=128))
        queued = provider.to_result(_file("a/b/d.flac", free=False))
        assert provider._rank(free) > provider._rank(queued)

    def test_an_empty_query_asks_nothing(self, provider):
        assert provider.search("   ") == []

    def test_the_limit_is_respected(self, provider):
        assert len(provider.search("track", 1)) == 1

    def test_availability_is_reported(self, provider):
        assert all(r.availability in ("free", "queued")
                   for r in provider.search("track", 10))


class TestResultMapping:
    def test_the_peer_and_path_identify_the_file(self):
        result = SK.SoulseekProvider().to_result(_file("C:\\Mus\\x.flac"))
        assert result.extra["username"] == "peer1"
        assert result.extra["filename"] == "C:\\Mus\\x.flac"

    def test_the_artist_is_guessed_from_the_folder(self):
        result = SK.SoulseekProvider().to_result(
            _file("Aphex Twin/Selected Ambient Works/01 Xtal.flac")
        )
        assert result.artist == "Aphex Twin"

    def test_a_flat_path_yields_no_artist(self):
        # Search results carry no tags at all — only a path — so a shallow one
        # gives nothing to guess from, and guessing wrong is worse than a dash.
        assert SK.SoulseekProvider().to_result(_file("track.flac")).artist is None

    def test_lossless_extensions_are_flagged(self):
        provider = SK.SoulseekProvider()
        assert provider.to_result(_file("a/b/c.flac")).extra["lossless"]
        assert not provider.to_result(_file("a/b/c.mp3")).extra["lossless"]

    def test_a_missing_extension_is_taken_from_the_name(self):
        result = SK.SoulseekProvider().to_result(_file("a/b/c.flac", extension=""))
        assert result.extra["extension"] == "flac"

    def test_a_zero_bitrate_reads_as_unknown_not_zero(self):
        result = SK.SoulseekProvider().to_result(_file("a/b/c.flac", bitrate=0))
        assert result.source_kbps is None
        assert result.display_bitrate == "—"


class TestProbe:
    def test_a_result_probes_to_one_stream(self, provider):
        result = provider.search("track", 10)[0]
        media = provider.probe(result)
        assert len(media.streams) == 1

    def test_a_flac_probes_as_lossless(self, provider):
        flac = next(r for r in provider.search("track", 10)
                    if r.title.endswith(".flac"))
        assert provider.probe(flac).lossless

    def test_an_mp3_does_not(self, provider):
        mp3 = next(r for r in provider.search("track", 10)
                   if r.title.endswith(".mp3"))
        assert not provider.probe(mp3).lossless

    def test_nothing_is_marked_upscale_against_a_lossless_peer(self, provider):
        flac = next(r for r in provider.search("track", 10)
                    if r.title.endswith(".flac"))
        media = provider.probe(flac)
        assert all(o.note is None for o in provider.format_options(media))


class TestRetryAgainstOtherPeers:
    def test_a_dead_peer_falls_through_to_the_next(self, tmp_path, monkeypatch):
        """Peers vanish mid-transfer as a matter of course, so a failure moves
        to the next copy rather than surfacing as an error."""
        provider = SK.SoulseekProvider(client=object())
        first = provider.to_result(_file("a/b/c.flac", username="dead"))
        second = provider.to_result(_file("a/b/c.flac", username="alive"))

        attempted: list[str] = []

        def fake_transfer(result, progress):
            attempted.append(result.extra["username"])
            if result.extra["username"] == "dead":
                raise RuntimeError("peer went offline")
            path = tmp_path / "got.flac"
            path.write_bytes(b"fLaC")
            return path

        monkeypatch.setattr(provider, "_transfer", fake_transfer)
        out = provider.fetch(first, config.ORIGINAL, tmp_path / "out",
                             alternatives=[second])
        assert attempted == ["dead", "alive"]
        assert out.exists()

    def test_every_peer_failing_raises_with_the_reason(self, tmp_path, monkeypatch):
        provider = SK.SoulseekProvider(client=object())
        result = provider.to_result(_file("a/b/c.flac"))
        monkeypatch.setattr(
            provider, "_transfer",
            lambda r, p: (_ for _ in ()).throw(RuntimeError("nobody home")),
        )
        with pytest.raises(SK.SoulseekUnavailable, match="nobody home"):
            provider.fetch(result, config.ORIGINAL, tmp_path / "out")

    def test_passthrough_keeps_the_peers_bytes(self, tmp_path, monkeypatch):
        provider = SK.SoulseekProvider(client=object())
        result = provider.to_result(_file("a/b/c.flac"))
        source = tmp_path / "from-peer.flac"
        source.write_bytes(b"fLaC" + b"\0" * 100)
        monkeypatch.setattr(provider, "_transfer", lambda r, p: source)

        out = provider.fetch(result, config.ORIGINAL, tmp_path / "kept")
        assert out.suffix == ".flac"
        assert out.read_bytes().startswith(b"fLaC")


class TestPreview:
    def test_preview_is_a_local_file_not_an_embed(self, tmp_path, monkeypatch):
        """There is no stream to scrub on a P2P network, which is why the
        button says "Fetch & preview"."""
        provider = SK.SoulseekProvider(client=object())
        result = provider.to_result(_file("a/b/c.flac"))
        source = tmp_path / "peer.flac"
        source.write_bytes(b"fLaC")
        monkeypatch.setattr(provider, "_transfer", lambda r, p: source)
        monkeypatch.setattr(provider, "paths_preview", lambda: tmp_path / "prev")

        preview = provider.preview(result)
        assert isinstance(preview, LocalFile)
        assert preview.temporary and preview.path.exists()
        assert provider.preview_requires_transfer


def _file(filename: str, *, username: str = "peer1", bitrate: int = 320,
          free: bool = True, extension: str | None = None):
    class F:
        pass

    f = F()
    f.username = username
    f.filename = filename
    f.size = 1000
    f.bitrate = bitrate
    f.length = 200
    f.extension = (Path(filename).suffix.lstrip(".")
                   if extension is None else extension)
    f.upload_speed = 500
    f.queue_length = 0
    f.free_upload_slot = free
    return f


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


@pytest.fixture
def bootstrap(tmp_path):
    return boot.SlskdBootstrap(config.Paths.under(tmp_path).ensure())


class TestInstallState:
    def test_a_fresh_machine_reports_not_installed(self, bootstrap, monkeypatch):
        monkeypatch.setattr(boot.shutil, "which", lambda name: None)
        state = bootstrap.status()
        assert not state.installed and not state.configured
        assert "58 MB" in state.detail

    def test_an_slskd_already_on_the_machine_is_used(self, bootstrap, monkeypatch):
        # No reason to download a second 58 MB copy, and theirs may already be
        # configured.
        monkeypatch.setattr(boot.shutil, "which", lambda name: "/usr/local/bin/slskd")
        assert bootstrap.installed()
        assert bootstrap.resolve_binary() == Path("/usr/local/bin/slskd")

    def test_our_own_copy_wins_over_one_on_the_path(self, bootstrap, monkeypatch):
        bootstrap.binary.parent.mkdir(parents=True, exist_ok=True)
        bootstrap.binary.write_bytes(b"#!/bin/sh\n")
        monkeypatch.setattr(boot.shutil, "which", lambda name: "/usr/local/bin/slskd")
        assert bootstrap.resolve_binary() == bootstrap.binary

    def test_the_download_url_names_this_platform(self, bootstrap):
        url = bootstrap.download_url()
        assert boot.VERSION in url and url.endswith(".zip")
        assert "osx" in url


class TestConfigGeneration:
    def _write(self, bootstrap, tmp_path, **kw):
        share = tmp_path / "Music"
        share.mkdir(exist_ok=True)
        params = dict(username="me", password="secret", share_dirs=[share])
        params.update(kw)
        return bootstrap.write_config(**params)

    def test_it_writes_a_config(self, bootstrap, tmp_path):
        path = self._write(bootstrap, tmp_path)
        assert path.exists()
        assert "soulseek:" in path.read_text()

    def test_an_awkward_password_reads_back_intact(self, bootstrap, tmp_path):
        import yaml

        path = self._write(bootstrap, tmp_path, password='we"ird: #pass')
        parsed = yaml.safe_load(path.read_text())
        assert parsed["soulseek"]["password"] == 'we"ird: #pass'

    def test_the_config_is_not_world_readable(self, bootstrap, tmp_path):
        path = self._write(bootstrap, tmp_path)
        assert path.stat().st_mode & 0o077 == 0

    def test_the_api_key_is_generated_and_kept(self, bootstrap, tmp_path):
        self._write(bootstrap, tmp_path)
        key = bootstrap.api_key()
        assert key and len(key) >= 32
        self._write(bootstrap, tmp_path)
        assert bootstrap.api_key() == key, "the key changed between writes"

    def test_the_api_key_file_is_not_world_readable(self, bootstrap, tmp_path):
        self._write(bootstrap, tmp_path)
        assert bootstrap.api_key_file.stat().st_mode & 0o077 == 0

    def test_the_daemon_is_bound_to_loopback_only(self, bootstrap, tmp_path):
        body = self._write(bootstrap, tmp_path).read_text()
        assert "127.0.0.1/32" in body
        assert "0.0.0.0" not in body

    def test_no_credentials_is_an_error(self, bootstrap, tmp_path):
        with pytest.raises(boot.SlskdError, match="username"):
            self._write(bootstrap, tmp_path, username="", password="")

    def test_no_shared_folder_is_an_error_that_says_why(self, bootstrap, tmp_path):
        with pytest.raises(boot.SlskdError, match="bans clients"):
            bootstrap.write_config("me", "pw", share_dirs=[])

    def test_a_nonexistent_shared_folder_is_an_error(self, bootstrap, tmp_path):
        with pytest.raises(boot.SlskdError, match="does not exist"):
            bootstrap.write_config("me", "pw", share_dirs=[tmp_path / "nope"])

    def test_configured_reports_the_truth(self, bootstrap, tmp_path):
        assert not bootstrap.configured()
        self._write(bootstrap, tmp_path)
        assert bootstrap.configured()


class TestArchiveSafety:
    def test_a_zip_escaping_its_directory_is_refused(self, bootstrap, tmp_path):
        """A zip entry may name any path it likes."""
        archive = tmp_path / "evil.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("../../escaped", "x")
        with pytest.raises(boot.SlskdError, match="unsafe path"):
            bootstrap._extract(archive)
        assert not (tmp_path.parent / "escaped").exists()

    def test_an_absolute_path_is_refused(self, bootstrap, tmp_path):
        archive = tmp_path / "abs.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("/etc/passwd", "x")
        with pytest.raises(boot.SlskdError, match="unsafe path"):
            bootstrap._extract(archive)

    def test_a_truncated_download_is_refused(self, bootstrap, monkeypatch):
        class Tiny:
            headers = {"content-length": "100"}

            def raise_for_status(self):
                pass

            def iter_content(self, chunk_size):
                yield b"\0" * 100

        monkeypatch.setattr(
            bootstrap, "_session", type("S", (), {"get": lambda *a, **k: Tiny()})()
        )
        with pytest.raises(boot.SlskdError, match="expected about"):
            bootstrap.install()


class TestLifecycle:
    def test_starting_without_a_binary_says_so(self, bootstrap, monkeypatch):
        monkeypatch.setattr(boot.shutil, "which", lambda name: None)
        with pytest.raises(boot.SlskdError, match="not installed"):
            bootstrap.start()

    def test_starting_without_a_config_says_so(self, bootstrap, monkeypatch):
        bootstrap.binary.parent.mkdir(parents=True, exist_ok=True)
        bootstrap.binary.write_bytes(b"#!/bin/sh\n")
        with pytest.raises(boot.SlskdError, match="not configured"):
            bootstrap.start()

    def test_a_busy_port_is_not_fought_over(self, bootstrap, tmp_path, monkeypatch):
        """Two slskds on one account would kick each other off the network in
        a loop."""
        share = tmp_path / "Music"
        share.mkdir()
        bootstrap.binary.parent.mkdir(parents=True, exist_ok=True)
        bootstrap.binary.write_bytes(b"#!/bin/sh\n")
        bootstrap.write_config("me", "pw", [share])
        monkeypatch.setattr(bootstrap, "port_in_use", lambda: True)
        with pytest.raises(boot.SlskdError, match="already listening"):
            bootstrap.start()

    def test_a_foreign_daemon_is_reported_as_running(self, bootstrap, monkeypatch):
        monkeypatch.setattr(bootstrap, "port_in_use", lambda: True)
        state = bootstrap.status()
        assert state.foreign and state.running

    def test_stopping_when_nothing_runs_is_harmless(self, bootstrap):
        bootstrap.stop()

    def test_it_stops_on_context_exit(self, bootstrap):
        with bootstrap:
            pass
        assert not bootstrap.is_running()

    def test_the_url_is_loopback(self, bootstrap):
        assert bootstrap.url.startswith("http://127.0.0.1:")


class TestAwkwardCredentials:
    """A password may contain anything. The config is serialised by PyYAML
    rather than by hand for exactly this reason."""

    @pytest.mark.parametrize(
        "password",
        ['pa"ss', "pa\\ss", "with: colon", "#hash", "  spaced  ", "@at",
         "*star", "line\nbreak", "ünïcode", "!!bang", "%percent"],
    )
    def test_it_survives_the_round_trip(self, bootstrap, tmp_path, password):
        import yaml

        share = tmp_path / "Music"
        share.mkdir(exist_ok=True)
        path = bootstrap.write_config("me", password, [share])
        parsed = yaml.safe_load(path.read_text())
        assert parsed["soulseek"]["password"] == password

    def test_a_path_with_spaces_survives(self, bootstrap, tmp_path):
        import yaml

        share = tmp_path / "My Music Folder"
        share.mkdir()
        path = bootstrap.write_config("me", "pw", [share])
        parsed = yaml.safe_load(path.read_text())
        assert parsed["shares"]["directories"] == [str(share.resolve())]
