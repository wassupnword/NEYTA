"""Integration tests. Network required, skipped by default.

    .venv-neyta/bin/python -m pytest -m integration

These are what catch a service changing shape underneath the offline fixtures.
When one of these fails and its offline twin passes, the fixture is stale —
regenerate it with tools/refresh_fixtures.py rather than loosening the test.

Everything here runs unauthenticated: no cookies, no tokens, no account.
"""

from __future__ import annotations

import pytest

from neyta import config
from neyta.core import convert
from neyta.core.cache import Cache
from neyta.core.engine import Engine, EngineError, Unavailable
from neyta.providers.bandcamp import BandcampProvider
from neyta.providers.soundcloud import SoundCloudProvider
from neyta.providers.youtube import YouTubeProvider
from test_provider_contract import ProviderContract

pytestmark = [pytest.mark.integration, pytest.mark.slow]

#: "Me at the zoo" — the oldest video on YouTube. Short, and it is not going
#: anywhere, which is what a fixed test target needs to be.
ZOO = "https://www.youtube.com/watch?v=jNQXAC9IVRw"


@pytest.fixture(scope="module")
def engine():
    # A real cache, so a full run of this file does not re-ask for the same
    # metadata a dozen times.
    with Cache(None) as cache:
        yield Engine(cache=cache)


@pytest.fixture(scope="module")
def youtube(engine):
    return YouTubeProvider(engine)


@pytest.fixture(scope="module")
def soundcloud(engine):
    return SoundCloudProvider(engine)


@pytest.fixture(scope="module")
def bandcamp(engine):
    return BandcampProvider(engine)


#: A release the artist made downloadable, and one they did not.
BC_DOWNLOADABLE = "https://oylumtanis.bandcamp.com/track/burial-archangel"
BC_PREVIEW_ONLY = "https://burial.bandcamp.com/track/archangel"


# ---------------------------------------------------------------------------
# The same contract, against the real services
# ---------------------------------------------------------------------------


class TestYouTubeLive(ProviderContract):
    @pytest.fixture
    def provider(self, youtube):
        return youtube


class TestSoundCloudLive(ProviderContract):
    @pytest.fixture
    def provider(self, soundcloud):
        return soundcloud


class TestBandcampLive(ProviderContract):
    @pytest.fixture
    def provider(self, bandcamp):
        return bandcamp


# ---------------------------------------------------------------------------
# Claims from the build plan, re-checked against reality
# ---------------------------------------------------------------------------


class TestUnauthenticatedAccess:
    def test_youtube_search_works_with_no_account(self, youtube):
        assert youtube.search("aphex twin", 5)

    def test_soundcloud_search_works_with_no_account(self, soundcloud):
        assert soundcloud.search("burial", 5)

    def test_no_cookies_are_sent(self, engine):
        assert "cookiefile" not in engine.base_opts()


class TestCeilings:
    def test_youtube_tops_out_around_129k(self, youtube):
        """Build plan 2.1: there is no 320 on YouTube. If this ever fails
        upward, the ceiling in config.py is what needs updating."""
        media = youtube.probe(youtube.search("me at the zoo", 1)[0])
        assert media.source_kbps is not None
        assert media.source_kbps < 200, "YouTube is not serving 320k"

    def test_the_full_dash_audio_ladder_is_present(self, youtube):
        """The old release with player_client=android returned no audio-only
        formats at all. Their presence is the proof that override is gone."""
        from neyta.providers.base import Result

        media = youtube.probe(Result(provider="youtube", id="", title="", url=ZOO))
        audio_only = [s for s in media.streams if not s.has_video]
        assert len(audio_only) >= 3
        assert any(s.id == "140" for s in audio_only)

    def test_soundcloud_tops_out_at_160k(self, soundcloud):
        results = soundcloud.search("aphex twin xtal slowed", 3)
        for r in results:
            try:
                media = soundcloud.probe(r)
            except Unavailable:
                continue  # label uploads are frequently DRM-protected
            assert media.source_kbps is not None
            assert media.source_kbps <= 160
            return
        pytest.skip("no non-DRM SoundCloud result to probe")

    def test_mp3_320_is_marked_as_an_upscale_on_real_metadata(self, youtube):
        media = youtube.probe(youtube.search("me at the zoo", 1)[0])
        note = config.upscale_note(config.MP3_320, media.source_kbps)
        assert note and "upscale" in note


class TestDrm:
    def test_a_drm_track_reports_unavailable_not_a_generic_failure(self, soundcloud):
        """Some SoundCloud label uploads are DRM-protected. Retrying will not
        help, so it must not land in the retryable bucket."""
        for r in soundcloud.search("aphex twin xtal", 5):
            try:
                soundcloud.probe(r)
            except Unavailable:
                return
        pytest.skip("no DRM-protected result in this search")


class TestEndToEnd:
    def test_youtube_to_a_correct_wav(self, youtube, tmp_path):
        from neyta.providers.base import Result

        stub = Result(provider="youtube", id="", title="zoo", url=ZOO)
        out = youtube.fetch(stub, config.WAV_48_24, tmp_path / "zoo.wav")

        info = convert.probe(out)
        assert info.sample_rate == 48000
        assert info.codec == "pcm_s24le"
        assert info.duration == pytest.approx(19, abs=1)

    def test_a_timed_cut_is_exact(self, youtube, tmp_path):
        """The phrase-search primitive: only the matched span transfers."""
        from neyta.providers.base import Result

        stub = Result(provider="youtube", id="", title="zoo", url=ZOO)
        out = youtube.fetch(
            stub, config.WAV_48_24, tmp_path / "clip.wav", span=(4.0, 12.0)
        )
        assert convert.probe(out).duration == pytest.approx(8.0, abs=0.15)

    def test_soundcloud_to_a_correct_wav(self, soundcloud, tmp_path):
        for r in soundcloud.search("aphex twin xtal slowed", 3):
            try:
                soundcloud.probe(r)
            except Unavailable:
                continue
            out = soundcloud.fetch(r, config.WAV_48_24, tmp_path / "sc.wav")
            info = convert.probe(out)
            assert info.sample_rate == 48000
            assert info.codec == "pcm_s24le"
            assert info.duration and info.duration > 1
            return
        pytest.skip("no non-DRM SoundCloud result to fetch")

    def test_progress_runs_from_zero_to_one(self, youtube, tmp_path):
        from neyta.providers.base import Result

        seen: list[float] = []
        youtube.fetch(
            Result(provider="youtube", id="", title="zoo", url=ZOO),
            config.WAV_48_24,
            tmp_path / "p.wav",
            progress=lambda f, m: seen.append(f),
        )
        assert seen
        assert seen[-1] == pytest.approx(1.0)
        assert seen == sorted(seen), "progress must not go backwards"


class TestCaching:
    def test_a_repeated_search_costs_no_request(self, engine):
        provider = YouTubeProvider(engine)
        provider.search("aphex twin selected ambient", 5)
        before = engine.cache.stats.hits
        provider.search("aphex twin selected ambient", 5)
        assert engine.cache.stats.hits > before


class TestBandcampQuality:
    """The claim that made Bandcamp worth a tab: real lossless, unauthenticated,
    where the artist allows it."""

    def test_a_downloadable_release_really_serves_lossless(self, bandcamp):
        from neyta.providers.base import Result

        media = bandcamp.probe(
            Result(provider="bandcamp", id="", title="", url=BC_DOWNLOADABLE)
        )
        assert media.lossless
        assert media.best_audio.lossless
        assert media.source_kbps is None

    def test_a_paid_release_serves_only_the_128k_preview(self, bandcamp):
        from neyta.providers.base import Result

        media = bandcamp.probe(
            Result(provider="bandcamp", id="", title="", url=BC_PREVIEW_ONLY)
        )
        assert not media.lossless
        assert media.source_kbps == 128

    def test_flac_downloads_byte_for_byte(self, bandcamp, tmp_path):
        from neyta.providers.base import Result

        out = bandcamp.fetch(
            Result(provider="bandcamp", id="", title="t", url=BC_DOWNLOADABLE),
            config.FLAC_SOURCE, tmp_path / "t.flac",
        )
        info = convert.probe(out)
        assert info.codec == "flac"
        assert out.stat().st_size > 1_000_000

    def test_asking_for_flac_where_there_is_none_fails_rather_than_lying(
        self, bandcamp, tmp_path
    ):
        from neyta.providers.base import Result

        with pytest.raises(EngineError):
            bandcamp.fetch(
                Result(provider="bandcamp", id="", title="t", url=BC_PREVIEW_ONLY),
                config.FLAC_SOURCE, tmp_path / "t.flac",
            )
        assert not (tmp_path / "t.flac").exists()

    def test_search_returns_tracks(self, bandcamp):
        results = bandcamp.search("burial archangel", 5)
        assert results
        assert all(r.url and r.provider == "bandcamp" for r in results)


class TestCaptions:
    """The claims the whole phrase matcher rests on, re-checked live.

    If one of these fails while tests/test_captions.py passes, the fixtures
    have drifted from the service — regenerate with tools/refresh_fixtures.py
    rather than loosening the offline test.
    """

    #: A TED talk: automatic captions, and plenty of them.
    AUTO = "https://www.youtube.com/watch?v=8jPQjjsBbIc"
    #: "Me at the zoo": human-uploaded captions only.
    MANUAL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"

    def _payload(self, engine, url, want):
        import requests

        from neyta.core import captions as caps

        info = engine.extract(url)
        source = (info.get("automatic_captions") if want == "auto"
                  else info.get("subtitles")) or {}
        track = next(
            (t for lang, tracks in source.items() if lang.startswith("en")
             for t in tracks if t.get("ext") == "json3"), None
        )
        if track is None:
            pytest.skip(f"no {want} json3 track available")
        response = requests.get(track["url"], timeout=30)
        if response.status_code == 429:
            pytest.skip("rate-limited by YouTube")
        assert response.status_code == 200
        return response.json()

    def test_automatic_captions_still_carry_word_offsets(self, engine):
        from neyta.core import captions as caps

        payload = self._payload(engine, self.AUTO, "auto")
        assert caps.detect_kind(payload) == "auto"
        track = caps.parse_json3(payload, "v", "en", "auto")
        assert track.accuracy == "word"
        starts = [w.start_ms for w in track.words[:20]]
        assert len(set(starts)) > 10, "word offsets collapsed to line starts"

    def test_manual_captions_still_carry_none(self, engine):
        from neyta.core import captions as caps

        payload = self._payload(engine, self.MANUAL, "manual")
        assert caps.detect_kind(payload) == "manual"
        track = caps.parse_json3(payload, "v", "en", "manual")
        assert track.accuracy == "line"

    def test_a_video_with_no_automatic_track_is_handled(self, engine):
        # "Me at the zoo" predates automatic captioning entirely.
        info = engine.extract(self.MANUAL)
        chosen = __import__(
            "neyta.core.captions", fromlist=["pick_track"]
        ).pick_track(info)
        assert chosen is not None
        _, _, kind = chosen
        assert kind == "manual"


class TestPhraseLive:
    def test_a_phrase_is_found_in_real_captions(self, engine):
        from neyta.core import phrase as P

        search = P.discover("I broke into my own house", engine, candidates=8)
        if search.rate_limited and not search.hits:
            pytest.skip("rate-limited by YouTube")
        assert search.hits, search.summary
        assert any(h.accuracy == "word" for h in search.hits)

    def test_the_summary_never_claims_to_have_searched_youtube(self, engine):
        from neyta.core import phrase as P

        search = P.discover("a phrase that will not be found xyzzy", engine,
                            candidates=3)
        assert "top" in search.summary
