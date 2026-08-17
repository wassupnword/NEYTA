"""Cache hit / miss / expiry.

Expiry is tested by injecting `now` rather than sleeping, so the suite stays
fast and does not go flaky on a loaded machine.
"""

from __future__ import annotations

import threading

from neyta.core.cache import CAPTIONS, PROBE, SEARCH, Cache


class TestBasics:
    def test_miss_on_empty(self, cache):
        assert cache.get(SEARCH, "nothing") is None
        assert cache.stats.misses == 1
        assert cache.stats.hits == 0

    def test_roundtrip(self, cache):
        cache.put(SEARCH, "k", {"a": [1, 2, 3]})
        assert cache.get(SEARCH, "k") == {"a": [1, 2, 3]}
        assert cache.stats.hits == 1

    def test_overwrite_replaces(self, cache):
        cache.put(SEARCH, "k", 1)
        cache.put(SEARCH, "k", 2)
        assert cache.get(SEARCH, "k") == 2
        assert cache.count(SEARCH) == 1

    def test_namespaces_are_isolated(self, cache):
        cache.put(SEARCH, "k", "search")
        cache.put(PROBE, "k", "probe")
        assert cache.get(SEARCH, "k") == "search"
        assert cache.get(PROBE, "k") == "probe"

    def test_delete(self, cache):
        cache.put(SEARCH, "k", 1)
        assert cache.delete(SEARCH, "k") is True
        assert cache.delete(SEARCH, "k") is False
        assert cache.get(SEARCH, "k") is None

    def test_unicode_and_nesting_survive(self, cache):
        value = {"title": "坂本龍一 — 音楽", "segs": [{"t": 0, "w": "ら"}]}
        cache.put(CAPTIONS, "k", value)
        assert cache.get(CAPTIONS, "k") == value

    def test_a_stored_none_reads_back_as_none(self, cache):
        # Known limitation, asserted so it stays deliberate: a stored None is
        # indistinguishable from a miss at the `get` return value. Callers that
        # need the difference check `count` or the stats counters. No caller
        # currently stores None.
        cache.put(SEARCH, "k", None)
        assert cache.count(SEARCH) == 1
        assert cache.get(SEARCH, "k") is None
        assert cache.stats.hits == 1 and cache.stats.misses == 0


class TestExpiry:
    def test_entry_within_ttl_hits(self, cache):
        cache.put(SEARCH, "k", 1, now=1000.0)
        assert cache.get(SEARCH, "k", ttl=100, now=1050.0) == 1
        assert cache.stats.expirations == 0

    def test_entry_past_ttl_misses_and_counts_as_expiry(self, cache):
        cache.put(SEARCH, "k", 1, now=1000.0)
        assert cache.get(SEARCH, "k", ttl=100, now=1200.0) is None
        assert cache.stats.expirations == 1
        assert cache.stats.misses == 1

    def test_expired_entry_is_removed_on_read(self, cache):
        cache.put(SEARCH, "k", 1, now=1000.0)
        cache.get(SEARCH, "k", ttl=10, now=2000.0)
        assert cache.count(SEARCH) == 0

    def test_captions_never_expire(self, cache):
        # A published video's transcript does not change. Build plan 5.3.
        cache.put(CAPTIONS, "vid|en|auto", {"segs": []}, now=0.0)
        far_future = 60 * 60 * 24 * 365 * 10
        assert cache.get(CAPTIONS, "vid|en|auto", now=far_future) is not None
        assert cache.stats.expirations == 0

    def test_explicit_none_ttl_overrides_the_namespace_default(self, cache):
        cache.put(SEARCH, "k", 1, now=0.0)
        assert cache.get(SEARCH, "k", ttl=None, now=1e9) == 1

    def test_purge_expired_leaves_captions_alone(self, cache):
        cache.put(CAPTIONS, "c", 1, now=0.0)
        cache.put(SEARCH, "s", 1, now=0.0)
        cache.put(PROBE, "p", 1, now=0.0)
        removed = cache.purge_expired(now=1e9)
        assert removed == 2
        assert cache.count(CAPTIONS) == 1
        assert cache.count(SEARCH) == 0

    def test_purge_expired_spares_fresh_entries(self, cache):
        cache.put(SEARCH, "s", 1, now=1000.0)
        assert cache.purge_expired(now=1001.0) == 0
        assert cache.count(SEARCH) == 1


class TestPurge:
    def test_purge_one_namespace(self, cache):
        cache.put(SEARCH, "a", 1)
        cache.put(PROBE, "b", 1)
        assert cache.purge(SEARCH) == 1
        assert cache.count(SEARCH) == 0
        assert cache.count(PROBE) == 1

    def test_purge_everything(self, cache):
        for ns in (SEARCH, PROBE, CAPTIONS):
            cache.put(ns, "k", 1)
        assert cache.purge() == 3
        assert cache.count() == 0


class TestTypedHelpers:
    def test_caption_helpers_agree_on_their_key(self, cache):
        cache.put_captions("dQw4w9WgXcQ", "en", {"segs": [1]})
        assert cache.get_captions("dQw4w9WgXcQ", "en") == {"segs": [1]}

    def test_caption_kinds_do_not_collide(self, cache):
        # Auto and manual captions for one video are different data with
        # different timing accuracy (build plan 2.3). They must not overwrite
        # each other.
        cache.put_captions("v", "en", {"word_timing": True}, kind="auto")
        cache.put_captions("v", "en", {"word_timing": False}, kind="manual")
        assert cache.get_captions("v", "en", "auto") == {"word_timing": True}
        assert cache.get_captions("v", "en", "manual") == {"word_timing": False}

    def test_search_key_is_case_and_whitespace_insensitive(self, cache):
        cache.put_search("youtube", "  Aphex Twin ", 30, ["a"])
        assert cache.get_search("youtube", "aphex twin", 30) == ["a"]

    def test_search_key_respects_limit_and_provider(self, cache):
        cache.put_search("youtube", "q", 30, ["a"])
        assert cache.get_search("youtube", "q", 10) is None
        assert cache.get_search("soundcloud", "q", 30) is None

    def test_probe_helpers(self, cache):
        cache.put_probe("soundcloud", "12345", {"streams": []})
        assert cache.get_probe("soundcloud", "12345") == {"streams": []}
        assert cache.get_probe("youtube", "12345") is None


class TestPersistence:
    def test_survives_reopening(self, paths):
        with Cache(paths.cache_db) as c:
            c.put_captions("v", "en", {"segs": [1, 2]})
        with Cache(paths.cache_db) as c:
            assert c.get_captions("v", "en") == {"segs": [1, 2]}

    def test_creates_its_parent_directory(self, tmp_path):
        target = tmp_path / "deep" / "nested" / "cache.sqlite3"
        with Cache(target):
            pass
        assert target.exists()


class TestConcurrency:
    def test_parallel_writers_do_not_corrupt(self, cache):
        # Caption fetching runs four workers wide; the cache is written from
        # all of them.
        def writer(n):
            for i in range(50):
                cache.put(SEARCH, f"{n}-{i}", {"n": n, "i": i})

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert cache.count(SEARCH) == 200
        assert cache.get(SEARCH, "3-49") == {"n": 3, "i": 49}


class TestStats:
    def test_hit_rate(self, cache):
        cache.put(SEARCH, "k", 1)
        cache.get(SEARCH, "k")
        cache.get(SEARCH, "missing")
        assert cache.stats.lookups == 2
        assert cache.stats.hit_rate == 0.5

    def test_hit_rate_of_an_untouched_cache_is_zero_not_an_error(self, cache):
        assert cache.stats.hit_rate == 0.0
