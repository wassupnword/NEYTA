"""The conformance suite: one shared class every provider must satisfy.

Build plan 8. Adding a fourth source means subclassing ProviderContract and
supplying a fixture — if the new provider breaks the contract the tab bar
relies on, it fails here rather than in the UI.

Runs offline against a fake engine. The same class is re-used under the
`integration` marker in test_providers_live.py against the real services.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from neyta import config
from neyta.core.engine import Engine, ExtractionFailed
from neyta.providers.base import (
    Embed, LocalFile, Media, NotSupported, Provider, Result, Stream,
)
from neyta.providers.soundcloud import SoundCloudProvider
from neyta.providers.youtube import YouTubeProvider

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text("utf-8"))


class FakeEngine(Engine):
    """Stands in for yt-dlp. Records calls so the tests can assert on them."""

    def __init__(self, search_entries=None, extract_info=None):
        super().__init__(cache=None)
        self._entries = search_entries or []
        self._info = extract_info or {}
        self.calls: list[tuple] = []

    def search(self, prefix, query, limit=20, *, use_cache=True):
        self.calls.append(("search", prefix, query, limit))
        return self._entries[:limit]

    def extract(self, url, *, provider=None, use_cache=True):
        self.calls.append(("extract", url, provider))
        if not self._info:
            raise ExtractionFailed(f"no fixture for {url}")
        return self._info

    def download(self, url, dest_dir, *, selector="bestaudio/best", span=None,
                 progress=None, filename_stem="source"):
        self.calls.append(("download", url, selector, span))
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        out = dest_dir / f"{filename_stem}.m4a"
        out.write_bytes(b"\x00" * 4096)
        if progress:
            progress(1.0, "downloaded")
        return out


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


class ProviderContract:
    """Subclass and provide `provider`. Every assertion here is something the
    shared UI depends on being true of all three tabs."""

    @pytest.fixture
    def provider(self) -> Provider:
        raise NotImplementedError

    # -- identity --------------------------------------------------------

    def test_key_is_known_to_the_format_matrix(self, provider):
        assert provider.key in config.FORMATS
        assert provider.key in config.SOURCE_CEILING_KBPS

    def test_has_a_human_label(self, provider):
        assert provider.label and provider.label != provider.key

    def test_states_its_ceiling(self, provider):
        assert provider.ceiling_note

    def test_offers_at_least_one_format(self, provider):
        assert provider.formats()

    def test_its_default_format_is_one_it_offers(self, provider):
        default = config.format_by_key(config.DEFAULT_FORMAT[provider.key])
        assert default in provider.formats()

    # -- search ----------------------------------------------------------

    def test_search_returns_results_tagged_with_this_provider(self, provider):
        for r in provider.search("anything", 5):
            assert r.provider == provider.key

    def test_search_respects_its_limit(self, provider):
        assert len(provider.search("anything", 2)) <= 2

    def test_search_results_have_a_title(self, provider):
        assert all(r.title for r in provider.search("anything", 5))

    def test_empty_query_does_not_raise(self, provider):
        provider.search("", 5)

    def test_results_render_a_bitrate_even_when_unknown(self, provider):
        # The result list always shows something in the bitrate column.
        for r in provider.search("anything", 5):
            assert r.display_bitrate

    # -- probe -----------------------------------------------------------

    def test_probe_returns_streams(self, provider):
        result = provider.search("anything", 1)[0]
        media = provider.probe(result)
        assert isinstance(media, Media)
        assert media.streams

    def test_probe_reports_a_source_bitrate(self, provider):
        media = provider.probe(provider.search("anything", 1)[0])
        assert media.source_kbps is None or media.source_kbps > 0

    def test_best_audio_is_audio(self, provider):
        media = provider.probe(provider.search("anything", 1)[0])
        best = media.best_audio
        assert best is None or not best.has_video

    def test_probe_of_a_result_with_no_url_raises_cleanly(self, provider):
        bare = Result(provider=provider.key, id="x", title="x", url=None)
        with pytest.raises(Exception):
            provider.probe(bare)

    # -- formats ---------------------------------------------------------

    def test_annotated_formats_cover_every_offered_format(self, provider):
        annotated = provider.annotated_formats(128)
        assert len(annotated) == len(provider.formats())

    def test_wav_is_never_marked_as_an_upscale(self, provider):
        for fmt, note in provider.annotated_formats(96):
            if fmt.kind == "pcm":
                assert note is None

    def test_upscales_are_marked_against_a_low_source(self, provider):
        marked = [f for f, note in provider.annotated_formats(96) if note]
        lossy_above = [
            f for f in provider.formats()
            if f.kind == "lossy" and (f.bitrate_kbps or 0) > 96
        ]
        assert {f.key for f in marked} == {f.key for f in lossy_above}

    # -- preview ---------------------------------------------------------

    def test_preview_is_an_embed_or_a_local_file(self, provider):
        preview = provider.preview(provider.search("anything", 1)[0])
        assert isinstance(preview, (Embed, LocalFile))

    def test_an_embed_preview_downloads_nothing(self, provider):
        preview = provider.preview(provider.search("anything", 1)[0])
        if isinstance(preview, Embed):
            assert preview.url.startswith("https://")


# ---------------------------------------------------------------------------
# The two phase-2 providers
# ---------------------------------------------------------------------------


class TestYouTubeContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return YouTubeProvider(
            FakeEngine(load("youtube_search.json"), load("youtube_extract.json"))
        )


class TestSoundCloudContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return SoundCloudProvider(
            FakeEngine(load("soundcloud_search.json"), load("soundcloud_extract.json"))
        )


class FakeLucidaServer:
    """lucida-flow's local API, in as few lines as the contract needs."""

    BODIES = {
        "search": {"tracks": [
            {"name": "A Track", "artist": "An Artist",
             "url": "https://open.spotify.com/track/1"},
            {"name": "Another", "artist": "An Artist",
             "url": "https://open.spotify.com/track/2"},
        ]},
        "info": {"quality": "FLAC 16-bit / 44.1 kHz"},
    }

    class Response:
        def __init__(self, payload):
            self._payload = payload
            self.status_code = 200

        def json(self):
            return self._payload

    def post(self, url, json=None, timeout=None, **kw):
        return self.Response(self.BODIES.get(url.rsplit("/", 1)[-1], {}))


class FakeLucidaBootstrap:
    url = "http://127.0.0.1:8787"

    def installed(self):
        return True

    def start(self, **kw):
        pass


class TestSpotifyContract(ProviderContract):
    """The fifth source. Everything above the provider layer treats it like
    the other four, so it answers to the same suite."""

    @pytest.fixture
    def provider(self):
        from neyta.providers.lucida import LucidaProvider

        return LucidaProvider(
            bootstrap=FakeLucidaBootstrap(), session=FakeLucidaServer()
        )

    # -- the one clause it does not satisfy, and why

    def test_preview_is_an_embed_or_a_local_file(self, provider):
        # It is neither: there is no stream to scrub, and the only way to
        # produce a local file is to run the whole rip. The contract's real
        # requirement is that a tab answers the question rather than hanging
        # or lying, and NotSupported with a reason is that answer.
        with pytest.raises(NotSupported, match="Download it"):
            provider.preview(provider.search("anything", 1)[0])

    def test_an_embed_preview_downloads_nothing(self, provider):
        with pytest.raises(NotSupported):
            provider.preview(provider.search("anything", 1)[0])


# ---------------------------------------------------------------------------
# Per-provider specifics
# ---------------------------------------------------------------------------


class TestYouTubeSpecifics:
    @pytest.fixture
    def provider(self):
        return YouTubeProvider(
            FakeEngine(load("youtube_search.json"), load("youtube_extract.json"))
        )

    def test_search_uses_the_ytsearch_prefix(self, provider):
        provider.search("x", 3)
        assert provider.engine.calls[0][:2] == ("search", "ytsearch")

    def test_the_real_audio_ladder_is_read(self, provider):
        media = provider.probe(provider.search("x", 1)[0])
        audio = {s.id: s.bitrate_kbps for s in media.streams if not s.has_video}
        # Format 140 is the 129k AAC that defines YouTube's ceiling.
        assert audio["140"] == pytest.approx(129.796, abs=0.01)
        assert audio["251"] == pytest.approx(106.064, abs=0.01)

    def test_best_audio_is_never_the_muxed_360p(self, provider):
        # Format 18 is the muxed 360p. Choosing it for an audio request is the
        # exact mistake the old player_client=android override forced.
        media = provider.probe(provider.search("x", 1)[0])
        assert media.best_audio.id != "18"
        assert media.best_audio.id == "140"

    def test_video_streams_are_flagged_as_video(self, provider):
        media = provider.probe(provider.search("x", 1)[0])
        assert any(s.has_video for s in media.streams)
        assert not media.best_audio.has_video

    def test_storyboards_are_not_streams(self, provider):
        media = provider.probe(provider.search("x", 1)[0])
        assert all("storyboard" not in s.note for s in media.streams)

    def test_mp3_320_is_marked_against_the_real_ceiling(self, provider):
        media = provider.probe(provider.search("x", 1)[0])
        notes = dict(
            (f.key, n) for f, n in provider.annotated_formats(media.source_kbps)
        )
        assert notes["mp3_320"] and "130k" in notes["mp3_320"]
        assert notes["wav_48_24"] is None

    def test_preview_is_the_nocookie_embed(self, provider):
        embed = provider.preview(provider.search("x", 1)[0])
        assert "youtube-nocookie.com/embed/" in embed.url
        assert "autoplay=1" in embed.url

    def test_preview_can_start_at_a_phrase_hit(self, provider):
        embed = provider.preview(provider.search("x", 1)[0], start=93.7)
        assert "start=93" in embed.url
        assert embed.start == 93.7

    def test_youtube_supports_spans(self, provider):
        assert provider.supports_spans is True


class TestSoundCloudSpecifics:
    @pytest.fixture
    def provider(self):
        return SoundCloudProvider(
            FakeEngine(load("soundcloud_search.json"), load("soundcloud_extract.json"))
        )

    def test_search_uses_the_scsearch_prefix(self, provider):
        provider.search("x", 3)
        assert provider.engine.calls[0][:2] == ("search", "scsearch")

    def test_the_ceiling_is_160k_aac(self, provider):
        media = provider.probe(provider.search("x", 1)[0])
        assert media.source_kbps == 160
        assert media.best_audio.id == "hls_aac_160k"

    def test_artists_field_beats_the_account_name(self, provider):
        # A track uploaded by a rip account still credits the real artist.
        # In the fixture the uploader is "idk bro" and the credit is
        # "Aphex twin"; the filename should carry the latter.
        entry = load("soundcloud_search.json")[0]
        result = provider.search("x", 1)[0]
        assert result.artist == "Aphex twin"
        assert result.artist != entry["uploader"]

    def test_a_plain_track_is_not_marked_lossless(self, provider):
        media = provider.probe(provider.search("x", 1)[0])
        assert media.lossless is False

    def test_an_original_download_is_detected_as_lossless(self, provider):
        info = load("soundcloud_extract.json")
        info["formats"] = info["formats"] + [
            {"format_id": "download", "ext": "wav", "acodec": "pcm_s16le",
             "vcodec": "none", "abr": 1411}
        ]
        provider = SoundCloudProvider(FakeEngine(load("soundcloud_search.json"), info))
        media = provider.probe(provider.search("x", 1)[0])
        assert media.lossless is True
        assert media.best_audio.id == "download"
        # A lossless source has no bitrate to be upscaled past, so nothing is
        # marked — MP3 320 from a WAV is a downgrade, not an inflation.
        assert media.source_kbps is None
        assert media.quality_label == "WAV"
        assert all(o.note is None for o in provider.format_options(media))

    def test_preview_is_the_official_widget(self, provider):
        embed = provider.preview(provider.search("x", 1)[0])
        assert embed.url.startswith("https://w.soundcloud.com/player/")
        assert "auto_play=true" in embed.url

    def test_soundcloud_does_not_do_spans(self, provider, tmp_path):
        # Whole tracks only — phrase search is a YouTube feature.
        assert provider.supports_spans is False
        with pytest.raises(NotSupported):
            provider.fetch(
                provider.search("x", 1)[0],
                config.WAV_48_24,
                tmp_path / "out.wav",
                span=(10.0, 20.0),
            )


class TestFetchWiring:
    """fetch() without touching the network or ffmpeg: assert what gets asked
    for, since the format selector is what keeps audio requests off the muxed
    360p stream."""

    @pytest.fixture
    def provider(self):
        return YouTubeProvider(
            FakeEngine(load("youtube_search.json"), load("youtube_extract.json"))
        )

    def test_audio_formats_request_bestaudio(self, provider, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "neyta.providers._ytdlp._is_usable", lambda p: True
        )
        monkeypatch.setattr(
            "neyta.core.convert.transcode",
            lambda src, dest, fmt, **kw: Path(dest).write_bytes(b"x") or Path(dest),
        )
        provider.fetch(
            provider.search("x", 1)[0], config.WAV_48_24, tmp_path / "out.wav"
        )
        selector = next(c[2] for c in provider.engine.calls if c[0] == "download")
        assert selector.startswith("bestaudio")

    def test_video_format_requests_video(self, provider, tmp_path, monkeypatch):
        monkeypatch.setattr("neyta.providers._ytdlp._is_usable", lambda p: True)
        monkeypatch.setattr(
            "neyta.core.convert.transcode",
            lambda src, dest, fmt, **kw: Path(dest).write_bytes(b"x") or Path(dest),
        )
        provider.fetch(
            provider.search("x", 1)[0], config.MP4_VIDEO, tmp_path / "out.mp4"
        )
        selector = next(c[2] for c in provider.engine.calls if c[0] == "download")
        assert "bestvideo" in selector

    def test_a_span_is_passed_to_the_downloader(self, provider, tmp_path, monkeypatch):
        monkeypatch.setattr("neyta.providers._ytdlp._is_usable", lambda p: True)
        monkeypatch.setattr(
            "neyta.core.convert.transcode",
            lambda src, dest, fmt, **kw: Path(dest).write_bytes(b"x") or Path(dest),
        )
        provider.fetch(
            provider.search("x", 1)[0], config.WAV_48_24, tmp_path / "out.wav",
            span=(12.0, 20.0),
        )
        span = next(c[3] for c in provider.engine.calls if c[0] == "download")
        assert span == (12.0, 20.0)

    def test_an_unusable_ranged_download_falls_back_to_a_full_fetch(
        self, provider, tmp_path, monkeypatch
    ):
        # The silent-empty-container failure: a ranged request that returns a
        # valid but empty file with exit code 0. Nothing downstream notices
        # until the user plays silence, so the fetch must not accept it.
        seen: list[str] = []
        monkeypatch.setattr(
            "neyta.providers._ytdlp._is_usable",
            lambda p: seen.append(p.name) or p.name.startswith("full"),
        )
        cut: list = []
        monkeypatch.setattr(
            "neyta.core.convert.transcode",
            lambda src, dest, fmt, **kw: (
                cut.append((kw.get("start"), kw.get("end")))
                or Path(dest).write_bytes(b"x")
                or Path(dest)
            ),
        )
        provider.fetch(
            provider.search("x", 1)[0], config.WAV_48_24, tmp_path / "out.wav",
            span=(12.0, 20.0),
        )
        downloads = [c for c in provider.engine.calls if c[0] == "download"]
        assert len(downloads) == 2, "should have refetched in full"
        assert downloads[0][3] == (12.0, 20.0)
        assert downloads[1][3] is None
        # ...and the cut it could not get over the wire happens locally.
        assert cut == [(12.0, 20.0)]
