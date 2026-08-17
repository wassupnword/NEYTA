"""json3 parsing and the two caption qualities.

The fixtures are trimmed captures of real tracks: an auto-generated one that
carries per-word offsets and a human-uploaded one that carries none. That
difference is the reason this module exists, so it is asserted directly rather
than assumed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from neyta.core import captions as C

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text("utf-8"))


@pytest.fixture
def auto_payload():
    return load("captions_auto.json")


@pytest.fixture
def manual_payload():
    return load("captions_manual.json")


class TestNormalise:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("House", "house"),
            ("don't", "dont"),
            ("Don’t", "dont"),          # a curly apostrophe is still elision
            ("minus 40°", "minus40"),
            ("  spaced  ", "spaced"),
            ("café", "cafe"),           # accents folded, so both spellings hit
            ("!!!", ""),
        ],
    )
    def test_folding(self, raw, expected):
        assert C.normalise(raw) == expected

    def test_tokenise_splits_and_drops_empties(self):
        assert C.tokenise("Don't  bother — asking!") == ["dont", "bother", "asking"]

    def test_tokenise_of_punctuation_only_is_empty(self):
        assert C.tokenise("!!! ... ???") == []


class TestAutoCaptions:
    def test_word_offsets_are_present(self, auto_payload):
        assert C.detect_kind(auto_payload) == "auto"

    def test_parsing_yields_word_accuracy(self, auto_payload):
        track = C.parse_json3(auto_payload, "vid", "en", "auto")
        assert track.accuracy == "word"
        assert track.tolerance_ms == 50

    def test_each_word_has_its_own_time(self, auto_payload):
        track = C.parse_json3(auto_payload, "vid", "en", "auto")
        starts = [w.start_ms for w in track.words[:6]]
        assert len(set(starts)) > 1, "words share a timestamp — offsets ignored"
        assert starts == sorted(starts)

    def test_the_first_word_of_an_event_starts_with_the_event(self, auto_payload):
        # It carries no tOffsetMs at all; absent means zero, not missing.
        track = C.parse_json3(auto_payload, "vid", "en", "auto")
        first_event = next(
            ev for ev in auto_payload["events"] if ev.get("segs")
            and any(s.get("utf8", "").strip() for s in ev["segs"])
        )
        assert track.words[0].start_ms == first_event["tStartMs"]

    def test_line_breaks_do_not_fuse_words(self, auto_payload):
        # The text contains "my own\nhouse"; matching must see two tokens.
        track = C.parse_json3(auto_payload, "vid", "en", "auto")
        assert all("\n" not in w.token for w in track.words)
        assert all(w.token for w in track.words)

    def test_rollup_events_contribute_no_words(self, auto_payload):
        # Auto-caption tracks carry `aAppend` events holding a lone newline,
        # present only to scroll the on-screen window.
        track = C.parse_json3(auto_payload, "vid", "en", "auto")
        assert all(w.text.strip() for w in track.words)


class TestManualCaptions:
    def test_no_word_offsets_anywhere(self, manual_payload):
        assert C.detect_kind(manual_payload) == "manual"

    def test_parsing_yields_line_accuracy(self, manual_payload):
        track = C.parse_json3(manual_payload, "vid", "en", "manual")
        assert track.accuracy == "line"
        assert track.tolerance_ms == 2000

    def test_every_word_in_a_line_shares_the_line_start(self, manual_payload):
        """The data has no word timing, so pretending otherwise would be a
        precision the file cannot support."""
        track = C.parse_json3(manual_payload, "vid", "en", "manual")
        by_line: dict[int, set[int]] = {}
        for word in track.words:
            by_line.setdefault(word.line_index, set()).add(word.start_ms)
        assert all(len(times) == 1 for times in by_line.values())

    def test_words_are_still_produced(self, manual_payload):
        track = C.parse_json3(manual_payload, "vid", "en", "manual")
        assert len(track.words) > 5

    def test_lines_have_an_end(self, manual_payload):
        track = C.parse_json3(manual_payload, "vid", "en", "manual")
        assert all(ln.end_ms > ln.start_ms for ln in track.lines)


class TestKindDetection:
    def test_the_data_wins_over_the_label(self):
        """A track filed under `automatic_captions` that carries no offsets is
        line-accurate whatever it claims to be."""
        payload = {"events": [
            {"tStartMs": 0, "dDurationMs": 1000, "segs": [{"utf8": "hello there"}]}
        ]}
        assert C.detect_kind(payload) == "manual"

    def test_one_offset_anywhere_is_enough(self):
        payload = {"events": [
            {"tStartMs": 0, "dDurationMs": 1000,
             "segs": [{"utf8": "a"}, {"utf8": " b", "tOffsetMs": 100}]}
        ]}
        assert C.detect_kind(payload) == "auto"

    def test_an_empty_payload_is_manual(self):
        assert C.detect_kind({}) == "manual"


class TestEdgeCases:
    def test_an_empty_payload_parses_to_an_empty_track(self):
        track = C.parse_json3({}, "vid", "en", "auto")
        assert track.words == () and track.lines == ()

    def test_the_window_definition_event_is_skipped(self):
        # The first event of an auto track has no segs at all.
        payload = {"events": [
            {"tStartMs": 0, "dDurationMs": 742399, "id": 1},
            {"tStartMs": 100, "dDurationMs": 500, "segs": [{"utf8": "word"}]},
        ]}
        track = C.parse_json3(payload, "vid", "en", "manual")
        assert len(track.lines) == 1

    def test_segs_of_pure_whitespace_are_skipped(self):
        payload = {"events": [
            {"tStartMs": 0, "dDurationMs": 100, "aAppend": 1,
             "segs": [{"utf8": "\n"}]},
            {"tStartMs": 100, "dDurationMs": 500, "segs": [{"utf8": "real"}]},
        ]}
        track = C.parse_json3(payload, "vid", "en", "manual")
        assert [w.text for w in track.words] == ["real"]

    def test_context_quotes_the_surrounding_words(self, auto_payload):
        track = C.parse_json3(auto_payload, "vid", "en", "auto")
        context = track.context(3, 5, span=2)
        assert len(context.split()) <= 8

    def test_a_track_round_trips_through_json(self, auto_payload):
        track = C.parse_json3(auto_payload, "vid", "en", "auto")
        again = C.CaptionTrack.from_json(track.to_json())
        assert again.words == track.words
        assert again.lines == track.lines
        assert again.accuracy == track.accuracy


class TestTrackSelection:
    def test_auto_is_preferred_over_manual(self):
        """The opposite of what you would want for reading, and exactly right
        here: only the automatic track has word timing."""
        info = {
            "automatic_captions": {"en": [{"ext": "json3", "url": "AUTO"}]},
            "subtitles": {"en": [{"ext": "json3", "url": "MANUAL"}]},
        }
        assert C.pick_track(info) == ("AUTO", "en", "auto")

    def test_manual_is_used_when_there_is_no_automatic_track(self):
        info = {"subtitles": {"en": [{"ext": "json3", "url": "MANUAL"}]}}
        assert C.pick_track(info) == ("MANUAL", "en", "manual")

    def test_only_json3_is_accepted(self):
        # srt and vtt carry no word offsets at all.
        info = {"subtitles": {"en": [{"ext": "vtt", "url": "V"},
                                     {"ext": "srt", "url": "S"}]}}
        assert C.pick_track(info) is None

    def test_an_english_variant_is_taken_when_plain_en_is_absent(self):
        info = {"automatic_captions": {"en-CA": [{"ext": "json3", "url": "CA"}]}}
        url, lang, kind = C.pick_track(info)
        assert url == "CA" and lang.startswith("en")

    def test_no_captions_yields_none(self):
        assert C.pick_track({"automatic_captions": {}, "subtitles": {}}) is None

    def test_a_track_with_no_url_is_not_chosen(self):
        info = {"subtitles": {"en": [{"ext": "json3"}]}}
        assert C.pick_track(info) is None
