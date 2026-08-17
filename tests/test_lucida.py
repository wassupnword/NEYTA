"""The Spotify tab and the lucida-flow bootstrap.

Offline throughout. A fake session plays lucida-flow's local HTTP API, and a
bootstrap pointed at a temporary directory stands in for the checkout, so
nothing here starts a server, a browser, or a download.

The bootstrap tests are mostly about what happens when lucida-flow is *not*
installed, because that is the state every user is in until they install it,
and "no results" would be the wrong thing to tell them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from neyta import config
from neyta.providers import lucida as LU
from neyta.providers.base import NotSupported, Result
from neyta.vendor import lucida_bootstrap as boot

SEARCH_BODY = {
    "query": "hotel california",
    "service": "spotify",
    "tracks": [
        {"name": "Hotel California", "artist": "Eagles",
         "album": "Hotel California",
         "url": "https://open.spotify.com/track/abc"},
        {"name": "Hotel California - Live", "artist": "Eagles",
         "album": "Hell Freezes Over",
         "url": "https://open.spotify.com/track/def"},
        # No URL: nothing can be done with it, so it never reaches the list.
        {"name": "Broken row", "artist": "Nobody"},
    ],
}


class FakeResponse:
    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeServer:
    """lucida-flow's API, as much of it as the provider uses."""

    def __init__(self, **bodies) -> None:
        self.bodies = bodies
        self.calls: list[tuple[str, dict]] = []
        self.status_code = 200

    def post(self, url, json=None, timeout=None, **kw):
        path = url.rsplit("/", 1)[-1]
        self.calls.append((path, json or {}))
        return FakeResponse(self.bodies.get(path, {}), self.status_code)


class FakeBootstrap:
    """Started or not; never actually launches anything."""

    def __init__(self, installed: bool = True) -> None:
        self._installed = installed
        self.url = "http://127.0.0.1:8787"
        self.starts = 0

    def installed(self) -> bool:
        return self._installed

    def start(self, **kw) -> None:
        if not self._installed:
            raise boot.LucidaError("not installed")
        self.starts += 1


@pytest.fixture
def server():
    return FakeServer(search=SEARCH_BODY)


@pytest.fixture
def provider(server):
    return LU.LucidaProvider(bootstrap=FakeBootstrap(), session=server)


# ---------------------------------------------------------------------------
# The tab
# ---------------------------------------------------------------------------


class TestTabBasics:
    def test_it_is_known_to_the_format_matrix(self):
        assert "spotify" in config.FORMATS
        assert config.SOURCE_CEILING_KBPS["spotify"] is None

    def test_it_defaults_to_keeping_the_file_the_service_had(self):
        assert config.DEFAULT_FORMAT["spotify"] == config.ORIGINAL.key

    def test_the_ceiling_note_refuses_to_promise_a_quality(self):
        note = config.CEILING_NOTE["spotify"]
        assert "property of the copy" in note

    def test_it_offers_lossless_because_the_lossless_tiers_exist(self):
        assert config.FLAC_SOURCE in config.formats_for("spotify")

    def test_whole_tracks_only(self):
        assert LU.LucidaProvider().supports_spans is False

    def test_asking_for_a_span_raises(self, provider, tmp_path):
        result = provider.search("hotel california", 1)[0]
        with pytest.raises(NotSupported):
            provider.fetch(result, config.ORIGINAL, tmp_path / "x",
                           span=(1.0, 2.0))

    def test_using_it_with_no_server_explains_itself(self):
        with pytest.raises(LU.LucidaUnavailable, match="Settings"):
            LU.LucidaProvider().search("anything")


class TestSearch:
    def test_it_returns_results_tagged_with_this_provider(self, provider):
        results = provider.search("hotel california", 5)
        assert results
        assert all(r.provider == "spotify" for r in results)

    def test_the_service_is_a_parameter_not_a_tab(self, provider, server):
        provider.search("hotel california", 5)
        assert server.calls[0][1]["service"] == config.LUCIDA_SERVICE

    def test_a_row_with_no_url_is_dropped(self, provider):
        # The URL is the id; a row without one cannot be probed or fetched.
        titles = [r.title for r in provider.search("hotel california", 5)]
        assert "Broken row" not in titles

    def test_the_url_is_the_id(self, provider):
        result = provider.search("hotel california", 1)[0]
        assert result.id == result.url == "https://open.spotify.com/track/abc"

    def test_search_respects_its_limit(self, provider):
        assert len(provider.search("hotel california", 1)) == 1

    def test_an_empty_query_asks_nothing(self, provider, server):
        assert provider.search("  ", 5) == []
        assert server.calls == []

    def test_a_row_shows_something_in_the_bitrate_column(self, provider):
        # Nothing here knows a bitrate before a probe, and the list still has
        # to render.
        assert all(r.display_bitrate for r in provider.search("x", 5))

    def test_the_server_is_started_on_demand_not_at_launch(self, provider):
        assert provider.bootstrap.starts == 0
        provider.search("hotel california", 1)
        assert provider.bootstrap.starts == 1

    def test_an_error_body_is_raised_with_its_own_message(self):
        server = FakeServer(search={"error": "lucida is down"})
        provider = LU.LucidaProvider(bootstrap=FakeBootstrap(), session=server)
        with pytest.raises(LU.LucidaUnavailable, match="lucida is down"):
            provider.search("x")


class TestProbe:
    def _probe(self, quality):
        server = FakeServer(search=SEARCH_BODY, info={"quality": quality})
        provider = LU.LucidaProvider(bootstrap=FakeBootstrap(), session=server)
        return provider.probe(provider.search("hotel california", 1)[0])

    def test_a_lossless_phrase_makes_a_lossless_stream(self):
        media = self._probe("FLAC 16-bit / 44.1 kHz")
        assert media.lossless
        assert media.has_lossless
        # A lossless source has no bitrate to beat, so nothing is marked as
        # an upscale against it.
        assert media.source_kbps is None

    def test_anything_else_is_treated_as_lossy(self):
        media = self._probe("320 kbps")
        assert not media.lossless

    def test_no_bitrate_is_invented_for_either(self):
        assert self._probe("320 kbps").streams[0].bitrate_kbps is None

    def test_the_services_own_words_are_kept_on_the_stream(self):
        assert self._probe("Hi-Res").streams[0].note == "Hi-Res"

    def test_probing_a_result_with_no_url_raises_cleanly(self, provider):
        bare = Result(provider="spotify", id="x", title="x", url=None)
        with pytest.raises(LU.LucidaUnavailable):
            provider.probe(bare)


class TestFetch:
    def test_the_services_file_is_kept_as_it_is(self, tmp_path):
        source = tmp_path / "downloaded.flac"
        source.write_bytes(b"fLaC" + b"\0" * 32)
        server = FakeServer(search=SEARCH_BODY, download={
            "success": True, "filepath": str(source), "size": 36,
        })
        provider = LU.LucidaProvider(bootstrap=FakeBootstrap(), session=server)
        result = provider.search("hotel california", 1)[0]

        written = provider.fetch(result, config.ORIGINAL, tmp_path / "out")
        assert written.read_bytes().startswith(b"fLaC")
        assert not source.exists(), "moved, not copied"

    def test_a_failed_download_says_what_the_server_said(self, tmp_path):
        server = FakeServer(search=SEARCH_BODY, download={
            "success": False, "error": "region locked",
        })
        provider = LU.LucidaProvider(bootstrap=FakeBootstrap(), session=server)
        result = provider.search("hotel california", 1)[0]
        with pytest.raises(LU.LucidaUnavailable, match="region locked"):
            provider.fetch(result, config.ORIGINAL, tmp_path / "out")

    def test_a_file_that_is_not_there_is_not_reported_as_success(self, tmp_path):
        server = FakeServer(search=SEARCH_BODY, download={
            "success": True, "filepath": str(tmp_path / "never written.flac"),
        })
        provider = LU.LucidaProvider(bootstrap=FakeBootstrap(), session=server)
        result = provider.search("hotel california", 1)[0]
        with pytest.raises(LU.LucidaUnavailable, match="not there"):
            provider.fetch(result, config.ORIGINAL, tmp_path / "out")


class TestPreview:
    def test_there_is_none_and_it_says_why(self, provider):
        result = provider.search("hotel california", 1)[0]
        with pytest.raises(NotSupported, match="Download it to hear it"):
            provider.preview(result)

    def test_asking_for_one_downloads_nothing(self, provider, server):
        result = provider.search("hotel california", 1)[0]
        before = len(server.calls)
        with pytest.raises(NotSupported):
            provider.preview(result)
        assert len(server.calls) == before


# ---------------------------------------------------------------------------
# The bootstrap
# ---------------------------------------------------------------------------


class TestBootstrap:
    def test_a_missing_checkout_is_the_normal_state_not_an_error(self, tmp_path):
        b = boot.LucidaBootstrap(root=tmp_path / "nothing here")
        assert not b.installed()
        assert not b.is_running()
        assert not b.status().installed

    def test_it_says_exactly_what_to_run(self, tmp_path):
        b = boot.LucidaBootstrap(root=tmp_path / "lucida-flow")
        hint = b.setup_hint
        assert "git clone" in hint
        assert "playwright install chromium" in hint
        assert str(b.root) in hint

    def test_starting_without_a_checkout_hands_back_that_hint(self, tmp_path):
        b = boot.LucidaBootstrap(root=tmp_path / "lucida-flow")
        with pytest.raises(boot.LucidaError, match="git clone"):
            b.start()

    def test_nothing_is_downloaded_or_installed_on_our_behalf(self, tmp_path):
        # Deliberate: this is a source checkout with a browser engine behind
        # it, not a signed binary NEYTA can fetch and size-check.
        assert not hasattr(boot.LucidaBootstrap(root=tmp_path), "install")

    def test_it_prefers_the_checkouts_own_interpreter(self, tmp_path):
        root = tmp_path / "lucida-flow"
        (root / ".venv" / "bin").mkdir(parents=True)
        venv_python = root / ".venv" / "bin" / "python"
        venv_python.write_text("#!/bin/sh\n")
        assert boot.LucidaBootstrap(root=root).python == venv_python

    def test_without_a_venv_it_falls_back_rather_than_failing(self, tmp_path):
        assert boot.LucidaBootstrap(root=tmp_path).python == Path("python3")

    def test_it_does_not_use_the_projects_own_busy_default_port(self):
        assert config.LUCIDA_PORT != 8000

    def test_the_server_is_bound_to_loopback(self):
        assert boot.BIND_ADDRESS == "127.0.0.1"

    def test_status_reads_as_a_sentence(self, tmp_path):
        state = boot.LucidaBootstrap(root=tmp_path / "gone").status()
        assert "not found" in state.detail

    def test_stopping_something_that_never_started_is_harmless(self, tmp_path):
        boot.LucidaBootstrap(root=tmp_path).stop()
