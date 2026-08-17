"""The Bandcamp tab.

Two fixtures on purpose: a release the artist made downloadable, and one they
did not. The ladder is a property of the release rather than of the service,
and almost everything interesting here follows from that.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests

from neyta import config
from neyta.core.engine import ExtractionFailed, format_selector
from neyta.providers.bandcamp import BandcampProvider
from neyta.providers.base import Media, Result, Stream
from test_provider_contract import FakeEngine, ProviderContract, load

FIXTURES = Path(__file__).parent / "fixtures"


class FakeSession:
    """Stands in for requests.Session, returning the captured API body."""

    def __init__(self, payload=None, exc=None):
        self.payload = payload
        self.exc = exc
        self.calls: list[dict] = []
        self.headers: dict = {}

    def post(self, url, json=None, timeout=None, **kw):
        self.calls.append({"url": url, "json": json})
        if self.exc:
            raise self.exc
        return FakeResponse(self.payload)


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        if self._payload is _BAD_JSON:
            raise ValueError("not json")
        return self._payload


_BAD_JSON = object()


def search_payload(hits=None):
    return {"auto": {"results": hits if hits is not None
                     else load("bandcamp_search.json")}}


def make(extract="bandcamp_extract_downloadable.json", hits=None, session=None):
    return BandcampProvider(
        FakeEngine([], load(extract)),
        session=session or FakeSession(search_payload(hits)),
    )


@pytest.fixture
def downloadable():
    return make("bandcamp_extract_downloadable.json")


@pytest.fixture
def preview_only():
    return make("bandcamp_extract_preview.json")


class TestBandcampContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return make()


class TestSearch:
    def test_it_uses_the_public_autocomplete_endpoint(self, downloadable):
        downloadable.search("burial", 5)
        call = downloadable._session.calls[0]
        assert "bcsearch_public_api" in call["url"]
        assert call["json"]["search_text"] == "burial"

    def test_it_asks_for_tracks_not_albums_or_artists(self, downloadable):
        downloadable.search("burial", 5)
        assert downloadable._session.calls[0]["json"]["search_filter"] == "t"

    def test_the_body_carries_the_fields_the_endpoint_demands(self, downloadable):
        # Omitting full_page/fan_id changes the response shape and the 'auto'
        # key disappears.
        body = downloadable.search("x", 3) and downloadable._session.calls[0]["json"]
        assert "full_page" in body and "fan_id" in body

    def test_results_are_tagged_bandcamp(self, downloadable):
        assert all(r.provider == "bandcamp" for r in downloadable.search("x", 5))

    def test_the_absolute_item_url_is_used_as_is(self, downloadable):
        # item_url_path is already a full URL. Joining it onto item_url_root
        # yields "https://x.bandcamp.comhttps://x.bandcamp.com/track/y".
        for r in downloadable.search("x", 5):
            assert r.url.startswith("https://")
            assert r.url.count("https://") == 1

    def test_band_name_becomes_the_artist(self, downloadable):
        hit = load("bandcamp_search.json")[0]
        assert downloadable.search("x", 1)[0].artist == hit["band_name"]

    def test_non_track_hits_are_dropped(self):
        hits = load("bandcamp_search.json")[:2] + [
            {"type": "a", "name": "An Album", "id": 1},
            {"type": "b", "name": "A Band", "id": 2},
        ]
        assert len(make(hits=hits).search("x", 10)) == 2

    def test_limit_is_respected(self, downloadable):
        assert len(downloadable.search("x", 2)) == 2

    def test_empty_query_makes_no_request(self, downloadable):
        assert downloadable.search("   ", 5) == []
        assert downloadable._session.calls == []

    def test_a_network_error_becomes_an_engine_error(self):
        provider = make(session=FakeSession(exc=requests.ConnectionError("down")))
        with pytest.raises(ExtractionFailed):
            provider.search("x", 5)

    def test_a_non_json_body_becomes_an_engine_error(self):
        provider = make(session=FakeSession(_BAD_JSON))
        with pytest.raises(ExtractionFailed):
            provider.search("x", 5)

    def test_a_missing_auto_key_is_not_a_crash(self):
        assert make(session=FakeSession({"tag": {}, "genre": {}})).search("x", 5) == []

    def test_results_are_cached(self, cache):
        session = FakeSession(search_payload())
        provider = BandcampProvider(
            FakeEngine([], load("bandcamp_extract_downloadable.json")), session=session
        )
        provider.engine.cache = cache
        first = provider.search("burial", 5)
        second = provider.search("burial", 5)
        assert [r.id for r in first] == [r.id for r in second]
        assert len(session.calls) == 1, "second search should have hit the cache"


class TestDownloadableRelease:
    """The artist enabled downloading: the full ladder is there."""

    def test_the_lossless_ladder_is_seen(self, downloadable):
        media = downloadable.probe(downloadable.search("x", 1)[0])
        ids = {s.id for s in media.streams}
        assert {"flac", "wav", "aiff-lossless", "falac"} <= ids

    def test_it_is_reported_as_lossless(self, downloadable):
        assert downloadable.probe(downloadable.search("x", 1)[0]).lossless is True

    def test_best_audio_is_lossless_not_the_128k_preview(self, downloadable):
        # A FLAC stream advertises no abr, so ranking on bitrate alone picks
        # the mp3-128 sitting beside it.
        best = downloadable.probe(downloadable.search("x", 1)[0]).best_audio
        assert best.lossless
        assert best.id != "mp3-128"

    def test_source_bitrate_is_none_so_nothing_is_marked(self, downloadable):
        media = downloadable.probe(downloadable.search("x", 1)[0])
        assert media.source_kbps is None
        assert all(o.note is None for o in downloadable.format_options(media))

    def test_mp3_320_from_a_lossless_source_is_not_an_upscale(self, downloadable):
        media = downloadable.probe(downloadable.search("x", 1)[0])
        assert not config.is_upscale(config.MP3_320, media.source_kbps)

    def test_flac_is_offered(self, downloadable):
        media = downloadable.probe(downloadable.search("x", 1)[0])
        flac = [o for o in downloadable.format_options(media)
                if o.format.key == "flac_source"][0]
        assert flac.available

    def test_the_quality_label_names_the_format(self, downloadable):
        media = downloadable.probe(downloadable.search("x", 1)[0])
        assert media.quality_label in {"FLAC", "WAV", "AIFF", "M4A"}


class TestPreviewOnlyRelease:
    """The artist did not enable downloading: a 128k stream and nothing else."""

    def test_only_the_preview_stream_exists(self, preview_only):
        media = preview_only.probe(preview_only.search("x", 1)[0])
        assert [s.id for s in media.streams] == ["mp3-128"]

    def test_it_is_not_reported_as_lossless(self, preview_only):
        assert preview_only.probe(preview_only.search("x", 1)[0]).lossless is False

    def test_the_ceiling_is_128k(self, preview_only):
        assert preview_only.probe(preview_only.search("x", 1)[0]).source_kbps == 128

    def test_mp3_320_is_marked_as_an_upscale(self, preview_only):
        media = preview_only.probe(preview_only.search("x", 1)[0])
        note = {o.format.key: o.note for o in preview_only.format_options(media)}
        assert note["mp3_320"] and "128k" in note["mp3_320"]
        assert note["mp3_128"] is None
        assert note["wav_48_24"] is None

    def test_flac_is_marked_unavailable_rather_than_silently_substituted(
        self, preview_only
    ):
        # Offering FLAC here and handing back a renamed MP3 is worse than an
        # error: you only notice after loading it into a session.
        media = preview_only.probe(preview_only.search("x", 1)[0])
        flac = [o for o in preview_only.format_options(media)
                if o.format.key == "flac_source"][0]
        assert not flac.available
        assert "not offered" in flac.note

    def test_asking_for_flac_selects_strictly_with_no_fallback(self):
        selector = format_selector(config.FLAC_SOURCE)
        assert "flac" in selector
        assert "/best" not in selector, "a fallback would yield an MP3 named .flac"


class TestLosslessDetection:
    @pytest.mark.parametrize(
        "codec,ext",
        [("flac", "flac"), ("alac", "m4a"), ("pcm_s16le", "wav"),
         ("aiff", "aiff"), (None, "flac"), (None, "wav")],
    )
    def test_lossless_streams_are_recognised(self, codec, ext):
        assert Stream(id="x", ext=ext, bitrate_kbps=None, codec=codec).lossless

    @pytest.mark.parametrize(
        "codec,ext",
        [("mp3", "mp3"), ("aac", "m4a"), ("opus", "webm"), ("vorbis", "ogg")],
    )
    def test_lossy_streams_are_not(self, codec, ext):
        assert not Stream(id="x", ext=ext, bitrate_kbps=128, codec=codec).lossless

    def test_lossless_outranks_a_higher_advertised_bitrate(self):
        media = Media(
            result=Result(provider="bandcamp", id="1", title="t"),
            streams=(
                Stream(id="mp3-320", ext="mp3", bitrate_kbps=320, codec="mp3"),
                Stream(id="flac", ext="flac", bitrate_kbps=None, codec="flac"),
            ),
        )
        assert media.best_audio.id == "flac"
        assert media.source_kbps is None
        assert media.quality_label == "FLAC"

    def test_video_streams_never_count_as_the_best_audio(self):
        media = Media(
            result=Result(provider="youtube", id="1", title="t"),
            streams=(
                Stream(id="18", ext="mp4", bitrate_kbps=96, codec="aac", has_video=True),
                Stream(id="140", ext="m4a", bitrate_kbps=129, codec="aac"),
            ),
        )
        assert media.best_audio.id == "140"

    def test_a_media_with_no_audio_has_no_label(self):
        media = Media(result=Result(provider="x", id="1", title="t"), streams=())
        assert media.best_audio is None
        assert media.quality_label == "—"
        assert media.source_kbps is None


class TestTabBasics:
    def test_it_has_no_fixed_ceiling(self):
        # Unlike YouTube and SoundCloud, this is per-release.
        assert config.SOURCE_CEILING_KBPS["bandcamp"] is None

    def test_the_ceiling_note_explains_both_cases(self):
        note = config.CEILING_NOTE["bandcamp"]
        assert "FLAC" in note and "128k" in note

    def test_it_defaults_to_keeping_the_artists_file(self):
        assert config.DEFAULT_FORMAT["bandcamp"] == config.ORIGINAL.key

    def test_no_video_option(self):
        assert all(f.kind != "video" for f in config.formats_for("bandcamp"))

    def test_whole_tracks_only(self, downloadable, tmp_path):
        from neyta.providers.base import NotSupported

        assert downloadable.supports_spans is False
        with pytest.raises(NotSupported):
            downloadable.fetch(
                downloadable.search("x", 1)[0], config.WAV_48_24,
                tmp_path / "o.wav", span=(1.0, 2.0),
            )

    def test_the_title_does_not_repeat_the_artist(self, downloadable):
        # yt-dlp's Bandcamp `title` is "Artist - Track"; using it would put
        # "oylumtanis - oylumtanis - Archangel.flac" on disk.
        media = downloadable.probe(downloadable.search("x", 1)[0])
        artist = media.result.artist
        assert artist
        assert not media.result.title.startswith(f"{artist} - ")

    def test_preview_is_the_official_embed(self, downloadable):
        result = downloadable.search("x", 1)[0]
        embed = downloadable.preview(result)
        assert embed.url.startswith("https://bandcamp.com/EmbeddedPlayer/track=")
        assert result.id in embed.url
        assert embed.autoplay is False
