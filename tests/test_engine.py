"""The yt-dlp facade: error classification, backoff, format selection, caching.

No network. yt-dlp itself is only touched through the option dictionary, which
is asserted rather than executed.
"""

from __future__ import annotations

import random

import pytest

from neyta import config
from neyta.core import engine as E
from neyta.core.cache import Cache


class TestClassify:
    @pytest.mark.parametrize(
        "message",
        [
            "HTTP Error 429: Too Many Requests",
            "ERROR: Unable to download API page: HTTP Error 429",
            "You have exceeded the rate limit",
            "rate-limited, try again later",
        ],
    )
    def test_rate_limiting_is_recognised(self, message):
        assert isinstance(E.classify(Exception(message)), E.RateLimited)

    @pytest.mark.parametrize(
        "message",
        [
            "ERROR: [youtube] abc: Video unavailable",
            "ERROR: [youtube] abc: Private video. Sign in if you've been granted access",
            "This video has been removed by the uploader",
            "Sign in to confirm your age",
            "ERROR: [soundcloud] 123: This video is DRM protected",
            "Requested format is not available",
            "ERROR: unable to download video data: HTTP Error 403: Forbidden",
        ],
    )
    def test_permanent_failures_are_recognised(self, message):
        assert isinstance(E.classify(Exception(message)), E.Unavailable)

    def test_a_403_on_the_media_url_is_permanent_not_retryable(self):
        """Measured at roughly 45% of samplette's library: metadata and the
        format ladder come back fine, then every player client 403s on the
        actual bytes. Retrying burns quota for nothing, and the shuffle path
        relies on this to know when to move to the next crate item."""
        err = E.classify(
            Exception("ERROR: unable to download video data: HTTP Error 403: Forbidden")
        )
        assert isinstance(err, E.Unavailable)
        assert not isinstance(err, E.RateLimited)

    def test_a_bare_403_elsewhere_stays_generic(self):
        # Only a 403 on the media fetch is known-permanent. A 403 somewhere
        # else may well be transient.
        assert not isinstance(
            E.classify(Exception("HTTP Error 403 while fetching the playlist")),
            E.Unavailable,
        )

    def test_anything_else_is_a_plain_failure(self):
        err = E.classify(Exception("connection reset by peer"))
        assert isinstance(err, E.ExtractionFailed)
        assert not isinstance(err, (E.RateLimited, E.Unavailable))

    def test_the_original_text_is_preserved(self):
        assert "peer" in str(E.classify(Exception("connection reset by peer")))

    def test_classification_is_case_insensitive(self):
        assert isinstance(E.classify(Exception("VIDEO UNAVAILABLE")), E.Unavailable)


class TestBackoff:
    def test_yields_the_requested_number_of_delays(self):
        assert len(list(E.backoff_delays(retries=5))) == 5

    def test_delays_grow(self):
        delays = list(E.backoff_delays(retries=5, jitter=0.0))
        assert delays == sorted(delays)
        assert delays[-1] > delays[0]

    def test_delays_are_capped(self):
        delays = list(E.backoff_delays(retries=20, base=3.0, cap=10.0, jitter=0.0))
        assert max(delays) <= 10.0

    def test_jitter_desynchronises_parallel_workers(self):
        # Four caption workers backing off by an identical amount come back as
        # one synchronised burst and get 429'd together again.
        a = list(E.backoff_delays(retries=4, rng=random.Random(1)))
        b = list(E.backoff_delays(retries=4, rng=random.Random(2)))
        assert a != b

    def test_jitter_stays_within_bounds(self):
        for delay in E.backoff_delays(retries=6, base=2.0, cap=1e9, jitter=0.25,
                                      rng=random.Random(0)):
            assert delay > 0


class TestFormatSelector:
    def test_audio_formats_never_request_video(self):
        for fmt in (config.WAV_48_24, config.WAV_44_16, config.MP3_320,
                    config.MP3_128, config.M4A_SOURCE):
            assert E.format_selector(fmt).startswith("bestaudio")

    def test_video_format_requests_video(self):
        assert "bestvideo" in E.format_selector(config.MP4_VIDEO)

    def test_m4a_source_prefers_the_native_container(self):
        assert "[ext=m4a]" in E.format_selector(config.M4A_SOURCE)

    def test_every_selector_has_a_fallback(self):
        # A service with no audio-only ladder must still yield something.
        for fmt in (config.WAV_48_24, config.MP3_320, config.MP4_VIDEO):
            assert "/" in E.format_selector(fmt)


class TestOptions:
    def test_no_cookies_by_default(self):
        opts = E.Engine().base_opts()
        assert "cookiefile" not in opts
        # Never reach into a browser's cookie store, even if asked nicely.
        assert "cookiesfrombrowser" not in opts

    def test_a_cookie_file_is_used_when_it_exists(self, tmp_path):
        jar = tmp_path / "cookies.txt"
        jar.write_text("# Netscape HTTP Cookie File\n")
        assert E.Engine(cookie_file=jar).base_opts()["cookiefile"] == str(jar)

    def test_a_missing_cookie_file_is_skipped_not_fatal(self, tmp_path):
        opts = E.Engine(cookie_file=tmp_path / "absent.txt").base_opts()
        assert "cookiefile" not in opts

    def test_no_player_client_override(self):
        """The old release only worked with player_client=android, and that
        override caps YouTube at the muxed 360p stream with no audio-only
        formats at all. Setting it would silently halve audio quality."""
        opts = E.Engine().base_opts()
        assert "extractor_args" not in opts

    def test_errors_are_not_swallowed(self):
        # ignoreerrors would turn a failed extraction into an empty result,
        # which reads as "no such track" rather than "something broke".
        assert E.Engine().base_opts()["ignoreerrors"] is False

    def test_certificate_checking_stays_on(self):
        assert E.Engine().base_opts()["nocheckcertificate"] is False

    def test_ffmpeg_is_advertised_as_a_full_path(self):
        # Not its directory: the only ffmpeg here is a symlink in a bin/ full
        # of unrelated executables, and yt-dlp resolves a directory by exact
        # basename.
        opts = E.Engine().base_opts()
        if "ffmpeg_location" in opts:
            assert opts["ffmpeg_location"].endswith("ffmpeg")


class TestSearchCaching:
    def _engine(self, cache, entries):
        eng = E.Engine(cache=cache)
        calls = []

        def fake(prefix, query, limit):
            calls.append((prefix, query, limit))
            return entries

        eng._raw_search = fake  # type: ignore[attr-defined]
        return eng, calls

    def test_an_empty_query_short_circuits(self, cache):
        assert E.Engine(cache=cache).search("ytsearch", "   ", 10) == []

    def test_results_are_cached_and_reused(self, cache, monkeypatch):
        calls = []

        def fake_call(self, fn, *a, **kw):
            calls.append(1)
            return {"entries": [{"id": "a", "title": "A"}]}

        monkeypatch.setattr(E.Engine, "_call", fake_call)
        eng = E.Engine(cache=cache)

        first = eng.search("ytsearch", "aphex", 5)
        second = eng.search("ytsearch", "aphex", 5)

        assert first == second == [{"id": "a", "title": "A"}]
        assert len(calls) == 1, "the second search should have come from cache"

    def test_the_cache_key_is_provider_scoped(self, cache, monkeypatch):
        monkeypatch.setattr(
            E.Engine, "_call",
            lambda self, fn, *a, **kw: {"entries": [{"id": "x", "title": "X"}]},
        )
        eng = E.Engine(cache=cache)
        eng.search("ytsearch", "q", 5)
        assert cache.get_search("youtube", "q", 5) is not None
        assert cache.get_search("soundcloud", "q", 5) is None

    def test_use_cache_false_bypasses_both_read_and_write(self, cache, monkeypatch):
        monkeypatch.setattr(
            E.Engine, "_call",
            lambda self, fn, *a, **kw: {"entries": [{"id": "x", "title": "X"}]},
        )
        eng = E.Engine(cache=cache)
        eng.search("ytsearch", "q", 5, use_cache=False)
        assert cache.get_search("youtube", "q", 5) is None

    def test_empty_entries_are_filtered(self, cache, monkeypatch):
        monkeypatch.setattr(
            E.Engine, "_call",
            lambda self, fn, *a, **kw: {"entries": [{"id": "a"}, None, {}]},
        )
        assert E.Engine(cache=cache).search("ytsearch", "q", 5) == [{"id": "a"}]

    def test_a_missing_entries_key_is_not_a_crash(self, cache, monkeypatch):
        monkeypatch.setattr(E.Engine, "_call", lambda self, fn, *a, **kw: {})
        assert E.Engine(cache=cache).search("ytsearch", "q", 5) == []


class TestRetryPolicy:
    def test_rate_limiting_is_retried(self, monkeypatch):
        monkeypatch.setattr(E.time, "sleep", lambda s: None)
        attempts = []

        def flaky():
            attempts.append(1)
            if len(attempts) < 3:
                raise Exception("HTTP Error 429: Too Many Requests")
            return "ok"

        assert E.Engine()._call(flaky) == "ok"
        assert len(attempts) == 3

    def test_permanent_failures_are_not_retried(self, monkeypatch):
        monkeypatch.setattr(E.time, "sleep", lambda s: None)
        attempts = []

        def gone():
            attempts.append(1)
            raise Exception("ERROR: Video unavailable")

        with pytest.raises(E.Unavailable):
            E.Engine()._call(gone)
        # Asking again will not make a deleted video exist.
        assert len(attempts) == 1

    def test_exhausted_backoff_raises_the_rate_limit_error(self, monkeypatch):
        monkeypatch.setattr(E.time, "sleep", lambda s: None)

        def always_limited():
            raise Exception("HTTP Error 429")

        with pytest.raises(E.RateLimited):
            E.Engine()._call(always_limited)
