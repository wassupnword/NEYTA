"""The Filmot phrase engine, offline.

No key and no network: a fake session answers with the shape the index
returns, so what is tested is the mapping from an index row to a Hit — which
is the part that has to be right for the panel, the player and the clip cutter
to work the same however the hit was found.
"""

from __future__ import annotations

import pytest

from neyta import config
from neyta.core import filmot
from neyta.core.phrase import PhraseSearch


class FakeResponse:
    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    """Records the call and answers with whatever it was given."""

    def __init__(self, payload, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code
        self.calls: list[dict] = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({
            "url": url, "params": params or {}, "headers": headers or {},
        })
        return FakeResponse(self.payload, self.status_code)


ROW = {
    "id": "abc123",
    "title": "A Talk About Everything",
    "channelname": "Some Channel",
    "duration": 1200,
    "hits": [
        {"start": 771.92, "ctx_before": "and then", "ctx_after": "before we go"},
        {"start": 15.5, "ctx_before": "", "ctx_after": "right"},
    ],
}


def index(payload, status_code: int = 200) -> filmot.FilmotIndex:
    return filmot.FilmotIndex(
        api_key="k", session=FakeSession(payload, status_code)
    )


class TestTheClient:
    def test_no_key_is_refused_before_any_request(self):
        with pytest.raises(filmot.FilmotUnavailable):
            filmot.FilmotIndex(api_key="")

    def test_the_key_goes_in_the_rapidapi_headers(self):
        session = FakeSession({"result": []})
        filmot.FilmotIndex(api_key="secret", session=session).search("hello")
        headers = session.calls[0]["headers"]
        assert headers["X-RapidAPI-Key"] == "secret"
        assert headers["X-RapidAPI-Host"] == filmot.DEFAULT_HOST

    def test_the_phrase_is_quoted_so_the_index_matches_it_exactly(self):
        session = FakeSession({"result": []})
        filmot.FilmotIndex(api_key="k", session=session).search("break it down")
        assert session.calls[0]["params"]["query"] == '"break it down"'

    def test_an_empty_phrase_asks_nothing(self):
        session = FakeSession({"result": []})
        assert filmot.FilmotIndex(api_key="k", session=session).search("  ") == []
        assert session.calls == []

    def test_a_refused_key_says_so_rather_than_failing_obscurely(self):
        with pytest.raises(filmot.FilmotUnavailable):
            index({}, status_code=403).search("x")

    def test_rate_limiting_is_its_own_message(self):
        with pytest.raises(filmot.FilmotError, match="rate-limited"):
            index({}, status_code=429).search("x")

    def test_a_body_that_is_not_json_is_an_error_not_a_crash(self):
        with pytest.raises(filmot.FilmotError):
            index(ValueError("nope")).search("x")

    def test_an_unexpected_shape_is_no_results_rather_than_an_exception(self):
        assert index({"result": "not a list"}).search("x") == []


class TestMappingARow:
    def test_every_occurrence_in_one_video_is_its_own_hit(self):
        # The point of an index over a ranking: one video, several places.
        hits = filmot.hits_from_row(ROW, "break it down")
        assert len(hits) == 2
        assert {h.video_id for h in hits} == {"abc123"}

    def test_the_start_is_carried_through_in_milliseconds(self):
        hit = filmot.hits_from_row(ROW, "break it down")[0]
        assert hit.start_ms == 771920
        assert hit.start == pytest.approx(771.92)

    def test_the_url_lands_you_at_the_timestamp(self):
        hit = filmot.hits_from_row(ROW, "break it down")[0]
        assert hit.url == "https://www.youtube.com/watch?v=abc123&t=771s"

    def test_hits_are_line_accurate_because_that_is_what_the_index_knows(self):
        # Filmot returns where a caption line starts, not where a word does.
        # Claiming word accuracy would put a word-accurate badge on a guess.
        hit = filmot.hits_from_row(ROW, "break it down")[0]
        assert hit.accuracy == "line"
        assert hit.badge == "line-accurate"
        assert hit.needs_trimming

    def test_the_end_is_estimated_from_the_length_of_the_phrase(self):
        # There is no end time in the response; the pad and the trim handles
        # are what correct this.
        hit = filmot.hits_from_row(ROW, "break it down")[0]
        assert hit.end_ms == 771920 + filmot.MS_PER_WORD * 3

    def test_the_context_reads_as_a_sentence_around_the_phrase(self):
        hit = filmot.hits_from_row(ROW, "break it down")[0]
        assert hit.context == "and then break it down before we go"

    def test_a_row_with_no_video_id_is_skipped(self):
        assert filmot.hits_from_row({"hits": [{"start": 1}]}, "x") == []

    def test_a_hit_with_no_timestamp_is_skipped(self):
        row = {"id": "v", "hits": [{"ctx_before": "no start here"}]}
        assert filmot.hits_from_row(row, "x") == []

    def test_alternative_field_spellings_are_accepted(self):
        # Filmot publishes no schema, so the fields are looked for under the
        # spellings that have been seen rather than one assumed name.
        row = {
            "videoid": "zzz", "name": "Another",
            "channel": "Chan", "subtitles": [{"time": 4}],
        }
        hit = filmot.hits_from_row(row, "hey")[0]
        assert (hit.video_id, hit.title, hit.uploader) == ("zzz", "Another", "Chan")
        assert hit.start_ms == 4000


class TestDiscover:
    def test_it_returns_the_same_type_the_builtin_engine_returns(self):
        search = filmot.discover("break it down", index({"result": [ROW]}))
        assert isinstance(search, PhraseSearch)
        assert search.engine == "filmot"
        assert len(search.hits) == 2

    def test_the_summary_says_which_engine_looked(self):
        search = filmot.discover("break it down", index({"result": [ROW]}))
        assert "Filmot's caption index" in search.summary
        # ...and never claims to have read the top N results.
        assert "top" not in search.summary

    def test_nothing_found_says_so_without_blaming_captions(self):
        search = filmot.discover("nothing", index({"result": []}))
        assert "nothing for this phrase" in search.summary

    def test_an_empty_phrase_does_not_call_the_service(self):
        session = FakeSession({"result": [ROW]})
        search = filmot.discover(
            "   ", filmot.FilmotIndex(api_key="k", session=session)
        )
        assert search.hits == []
        assert session.calls == []

    def test_cancelling_stops_before_the_request(self):
        session = FakeSession({"result": [ROW]})
        search = filmot.discover(
            "x", filmot.FilmotIndex(api_key="k", session=session),
            should_cancel=lambda: True,
        )
        assert search.hits == []
        assert session.calls == []

    def test_progress_ends_on_the_summary(self):
        seen: list[tuple[float, str]] = []
        filmot.discover(
            "break it down", index({"result": [ROW]}),
            progress=lambda f, m="": seen.append((f, m)),
        )
        assert seen[-1][0] == 1.0
        assert "index" in seen[-1][1]


class TestChoosingTheEngine:
    def test_the_free_engine_is_the_default(self):
        assert config.DEFAULT_PHRASE_ENGINE == "builtin"
        assert config.phrase_engine("builtin").free

    def test_filmot_is_declared_as_paid_and_names_its_service(self):
        option = config.phrase_engine("filmot")
        assert option.paid
        assert option.service == "filmot"

    def test_an_unknown_engine_key_raises(self):
        with pytest.raises(ValueError):
            config.phrase_engine("nope")
