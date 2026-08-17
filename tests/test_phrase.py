"""The matcher and the search-then-verify pipeline.

Runs offline against the caption fixtures. The point of most of these is that
a word-accurate hit and a line-accurate hit are handled differently all the
way through — different spans, different padding, different badges.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from neyta.core import captions as C
from neyta.core import phrase as P
from neyta.core.engine import RateLimited

FIXTURES = Path(__file__).parent / "fixtures"


def track(name: str, kind: str) -> C.CaptionTrack:
    payload = json.loads((FIXTURES / name).read_text("utf-8"))
    return C.parse_json3(payload, "vid", "en", kind)


@pytest.fixture
def auto():
    return track("captions_auto.json", "auto")


@pytest.fixture
def manual():
    return track("captions_manual.json", "manual")


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


class TestExactMatching:
    def test_a_multi_word_phrase_is_found(self, auto):
        assert P.find_in_track(auto, "I broke into my own")

    def test_the_match_spans_the_right_tokens(self, auto):
        first, last, score = P.find_in_track(auto, "broke into my own")[0]
        assert last - first == 3
        assert score == 1.0

    def test_matching_crosses_a_caption_line_break(self, auto):
        # The transcript reads "my own\nhouse"; a phrase spanning the break
        # must still match, which is why matching runs on a token stream.
        assert P.find_in_track(auto, "my own house")

    def test_case_is_ignored(self, auto):
        assert P.find_in_track(auto, "I BROKE INTO")

    def test_punctuation_is_ignored(self, auto):
        assert P.find_in_track(auto, "don't bother")
        assert P.find_in_track(auto, "dont bother")

    def test_extra_whitespace_is_ignored(self, auto):
        assert P.find_in_track(auto, "  broke   into  ")

    def test_a_phrase_that_is_not_there_finds_nothing(self, auto):
        assert P.find_in_track(auto, "purple monkey dishwasher") == []

    def test_an_empty_phrase_finds_nothing(self, auto):
        assert P.find_in_track(auto, "") == []
        assert P.find_in_track(auto, "   ") == []

    def test_a_phrase_longer_than_the_track_finds_nothing(self, auto):
        assert P.find_in_track(auto, " ".join(["word"] * 500)) == []

    def test_words_in_the_wrong_order_do_not_match(self, auto):
        # Adjacency is the whole claim being made about a hit.
        assert P.find_in_track(auto, "own my into broke", fuzzy=False) == []


class TestFuzzyMatching:
    def test_a_misheard_word_still_matches(self, auto):
        # The recogniser writes "house"; the user typed "home".
        hits = P.find_in_track(auto, "broke into my own home")
        assert hits and hits[0][2] < 1.0

    def test_fuzzy_can_be_turned_off(self, auto):
        assert P.find_in_track(auto, "broke into my own home", fuzzy=False) == []

    def test_an_exact_match_suppresses_the_fuzzy_pass(self, auto):
        hits = P.find_in_track(auto, "broke into my own")
        assert all(score == 1.0 for _, _, score in hits)

    def test_a_single_word_is_never_fuzzed(self, auto):
        # "house" would approximately match "horse", "hose", "mouse"...
        assert P.find_in_track(auto, "hause") == []

    def test_nonsense_does_not_squeak_past_the_threshold(self, auto):
        assert P.find_in_track(auto, "zzzz qqqq wwww") == []

    def test_overlapping_windows_are_collapsed(self, auto):
        hits = P.find_in_track(auto, "broke into my awn")
        spans = [(a, b) for a, b, _ in hits]
        for i, (a1, b1) in enumerate(spans):
            for a2, b2 in spans[i + 1:]:
                assert b1 < a2 or b2 < a1, "overlapping hits were both kept"


# ---------------------------------------------------------------------------
# Hits
# ---------------------------------------------------------------------------


class TestWordAccurateHits:
    @pytest.fixture
    def hit(self, auto):
        return P.hits_for(auto, "broke into my own",
                          title="T", url="U", uploader="Up")[0]

    def test_it_is_badged_word_accurate(self, hit):
        assert hit.accuracy == "word"
        assert hit.badge == "word-accurate"
        assert hit.tolerance_ms == 50

    def test_it_starts_on_the_word_not_the_line(self, auto, hit):
        line_start = auto.lines[0].start_ms
        assert hit.start_ms > line_start, "the hit fell back to the line start"

    def test_it_carries_context_and_the_matched_text(self, hit):
        assert "broke" in hit.matched
        assert len(hit.context) > len(hit.matched)

    def test_padding_is_small(self, hit):
        lo, hi = hit.padded()
        assert (hit.start - lo) == pytest.approx(P.DEFAULT_PAD)

    def test_it_does_not_ask_for_the_handles(self, hit):
        assert not hit.needs_trimming

    def test_the_span_is_never_zero_length(self, hit):
        assert hit.end_ms > hit.start_ms


class TestLineAccurateHits:
    @pytest.fixture
    def hit(self, manual):
        return P.hits_for(manual, "really long trunks", title="T", url="U")[0]

    def test_it_is_badged_line_accurate(self, hit):
        assert hit.accuracy == "line"
        assert hit.badge == "line-accurate"
        assert hit.tolerance_ms == 2000

    def test_it_asks_for_the_handles(self, hit):
        """A two-second window is a starting point you nudge, not an answer."""
        assert hit.needs_trimming

    def test_padding_covers_the_caption_tolerance(self, hit):
        # Cutting to a word timing this track does not have would clip the
        # phrase in half.
        lo, hi = hit.padded()
        assert (hit.start - lo) >= 2.0

    def test_padding_never_goes_negative(self, manual):
        hit = P.hits_for(manual, "All right", title="T", url="U")[0]
        lo, _ = hit.padded()
        assert lo == 0.0

    def test_the_end_comes_from_the_caption_line(self, manual, hit):
        line = manual.lines[hit_line(manual, hit)]
        assert hit.end_ms == line.end_ms


def hit_line(track: C.CaptionTrack, hit: P.Hit) -> int:
    for word in track.words:
        if word.start_ms == hit.start_ms:
            return word.line_index
    return 0


class TestHitFormatting:
    def test_the_label_is_a_timestamp(self, auto):
        hit = P.hits_for(auto, "broke into", title="T", url="U")[0]
        assert ":" in hit.label

    def test_duration_is_derived(self, auto):
        hit = P.hits_for(auto, "broke into", title="T", url="U")[0]
        assert hit.duration == pytest.approx((hit.end_ms - hit.start_ms) / 1000)


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------


class FakeEngine:
    def __init__(self, entries=None):
        self.entries = entries or []
        self.searches: list[str] = []
        self.cache = None

    def search(self, prefix, query, limit=20, **kw):
        self.searches.append(query)
        return self.entries[:limit]


class FakeFetcher:
    def __init__(self, tracks: dict[str, C.CaptionTrack | None], raises=None):
        self.tracks = tracks
        self.raises = raises or {}
        self.calls: list[str] = []

    def fetch(self, video_id, url):
        self.calls.append(video_id)
        if video_id in self.raises:
            raise self.raises[video_id]
        return self.tracks.get(video_id)


def entry(video_id: str, title: str = "A video"):
    return {"id": video_id, "title": title,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "uploader": "Someone"}


class TestQueryVariants:
    def test_the_phrase_is_quoted_first(self):
        assert P.query_variants("hello there")[0] == '"hello there"'

    def test_the_bare_phrase_is_also_tried(self):
        assert "hello there" in P.query_variants("hello there")

    def test_a_long_phrase_gets_a_shortened_variant(self):
        variants = P.query_variants("one two three four five six seven")
        assert "one two three four" in variants

    def test_a_short_phrase_gets_no_shortened_variant(self):
        assert len(P.query_variants("hello there")) == 2

    def test_an_empty_phrase_yields_nothing(self):
        assert P.query_variants("   ") == []


class TestDiscover:
    def test_an_empty_phrase_searches_nothing(self):
        engine = FakeEngine()
        result = P.discover("  ", engine)
        assert result.hits == [] and engine.searches == []

    def test_hits_are_collected_across_candidates(self, auto):
        engine = FakeEngine([entry("a"), entry("b")])
        fetcher = FakeFetcher({"a": auto, "b": auto})
        result = P.discover("broke into my own", engine, fetcher=fetcher, workers=2)
        assert len(result.hits) == 2
        assert result.searched == 2

    def test_candidates_are_deduplicated_across_variants(self, auto):
        engine = FakeEngine([entry("a"), entry("a"), entry("b")])
        result = P.discover(
            "broke into", engine, fetcher=FakeFetcher({"a": auto, "b": auto})
        )
        assert result.searched == 2

    def test_word_accurate_hits_are_ranked_first(self, auto, manual):
        engine = FakeEngine([entry("m"), entry("a")])
        # "really long trunks" only exists in the manual track; use a phrase
        # present in both so the ordering is about accuracy, not content.
        fetcher = FakeFetcher({"m": manual, "a": auto})
        result = P.discover("and", engine, fetcher=fetcher, fuzzy=False)
        accuracies = [h.accuracy for h in result.hits]
        assert accuracies == sorted(accuracies, key=lambda a: 0 if a == "word" else 1)

    def test_videos_without_captions_are_counted(self, auto):
        engine = FakeEngine([entry("a"), entry("b")])
        result = P.discover(
            "broke into", engine, fetcher=FakeFetcher({"a": auto, "b": None})
        )
        assert result.without_captions == 1

    def test_rate_limited_candidates_are_reported_separately(self, auto):
        """A 429 is not the same as "this video has no captions" — one is
        worth retrying in a minute and the other never is."""
        engine = FakeEngine([entry("a"), entry("b")])
        fetcher = FakeFetcher({"a": auto}, raises={"b": RateLimited("429")})
        result = P.discover("broke into", engine, fetcher=fetcher)
        assert result.rate_limited == 1
        assert "rate-limited" in result.summary

    def test_one_broken_candidate_does_not_sink_the_search(self, auto):
        engine = FakeEngine([entry("a"), entry("b")])
        fetcher = FakeFetcher({"a": auto}, raises={"b": RuntimeError("boom")})
        result = P.discover("broke into", engine, fetcher=fetcher)
        assert len(result.hits) == 1

    def test_cancellation_stops_early(self, auto):
        engine = FakeEngine([entry(str(i)) for i in range(20)])
        fetcher = FakeFetcher({str(i): auto for i in range(20)})
        result = P.discover(
            "broke into", engine, fetcher=fetcher, should_cancel=lambda: True
        )
        assert result.hits == []

    def test_progress_is_reported_and_ends_at_one(self, auto):
        seen: list[float] = []
        P.discover(
            "broke into", FakeEngine([entry("a")]),
            fetcher=FakeFetcher({"a": auto}),
            progress=lambda f, m="": seen.append(f),
        )
        assert seen and seen[-1] == 1.0

    def test_the_candidate_limit_is_respected(self, auto):
        engine = FakeEngine([entry(str(i)) for i in range(50)])
        fetcher = FakeFetcher({str(i): auto for i in range(50)})
        result = P.discover("broke into", engine, fetcher=fetcher, candidates=5)
        assert result.searched == 5


class TestSummary:
    def test_it_states_its_own_reach(self):
        """Never "searched YouTube" — this reads the top N results and says
        so, because that is what it did."""
        search = P.PhraseSearch(phrase="x", searched=30)
        assert "top 30 results" in search.summary
        assert "YouTube" not in search.summary.replace("rate-limited by YouTube", "")

    def test_it_counts_hits(self, auto):
        search = P.PhraseSearch(
            phrase="x", searched=3,
            hits=P.hits_for(auto, "broke into", title="T", url="U"),
        )
        assert "1 hit" in search.summary

    def test_an_empty_search_says_so(self):
        assert "no candidates" in P.PhraseSearch(phrase="x").summary

    def test_word_accurate_hits_can_be_isolated(self, auto, manual):
        search = P.PhraseSearch(
            phrase="x",
            hits=P.hits_for(auto, "broke into", title="T", url="U")
            + P.hits_for(manual, "really long", title="T", url="U"),
        )
        assert len(search.word_accurate) == 1
