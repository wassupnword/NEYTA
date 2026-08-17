"""Crate-digging over samplette-local's library.

The unit tests build a small library with known contents, so they assert exact
counts rather than "something came back". A handful of tests at the end run
against the real 73 MB file when it is present and skip when it is not.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from neyta import config
from neyta.core import samplette as S

SCHEMA = """
CREATE TABLE tracks (
    id INTEGER PRIMARY KEY, artist TEXT, title TEXT, release TEXT, year INTEGER,
    label TEXT, region TEXT, copyright TEXT, p_copyright TEXT, genres TEXT,
    styles TEXT, tags TEXT, musical_key TEXT, tempo REAL,
    discogs_release_id INTEGER, mb_recording_id TEXT, yt_video_id TEXT,
    yt_title TEXT, yt_channel TEXT, yt_channel_id TEXT, yt_is_topic INTEGER,
    yt_views INTEGER, yt_duration INTEGER, resolve_state TEXT,
    resolve_attempts INTEGER, enrich_state INTEGER, added_at REAL
);
"""

#: id, artist, title, year, region, genres, styles, key, tempo, video, topic,
#: views, duration, state
ROWS = [
    (1, "Jobim", "Chega De Saudade", 1959, "Brazil", ["Jazz"], ["Bossa Nova"],
     "D major", 144.0, "vid1", 1, 665559, 124, "ready"),
    (2, "Gal Costa", "Se Voce Pensa", 1978, "Brazil", ["Latin"], ["MPB"],
     "E minor", 102.0, "vid2", 0, 4099, 180, "ready"),
    (3, "Clara Nunes", "Pau De Arara", 1974, "Brazil", ["Latin"], ["Samba", "MPB"],
     "G# minor", 98.0, "vid3", 1, 39130, 200, "ready"),
    (4, "Johnny Cash", "I Walk The Line", 1967, "US", ["Rock"], ["Country"],
     "A# major", 114.0, "vid4", 0, 900000, 160, "ready"),
    (5, "Melvins", "Pearl Bomb", 1993, "Europe", ["Rock"], ["Grunge"],
     None, None, "vid5", 0, 5000, 240, "ready"),
    # No YouTube video yet: the crawler has not resolved it. 95% of the real
    # library looks like this.
    (6, "Unresolved", "Pending Track", 1980, "US", ["Jazz"], ["Free Jazz"],
     None, None, None, 0, None, None, "pending"),
    # Resolved but failed.
    (7, "Broken", "Failed Track", 1980, "US", ["Jazz"], ["Free Jazz"],
     None, None, None, 0, None, None, "failed"),
    # Untagged: genres/styles NULL. Exclude filters must not silently drop it.
    (8, "Untagged", "No Genre", 2001, None, None, None,
     None, 120.0, "vid8", 0, 10, 100, "ready"),
]


def build_library(path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    for (tid, artist, title, year, region, genres, styles, key, tempo,
         video, topic, views, duration, state) in ROWS:
        conn.execute(
            "INSERT INTO tracks (id, artist, title, release, year, label, region,"
            " genres, styles, tags, musical_key, tempo, discogs_release_id,"
            " mb_recording_id, yt_video_id, yt_title, yt_channel, yt_is_topic,"
            " yt_views, yt_duration, resolve_state, added_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, artist, title, f"{title} LP", year, "Some Label", region,
             json.dumps(genres) if genres else None,
             json.dumps(styles) if styles else None,
             json.dumps(styles) if styles else None,
             key, tempo, 1000 + tid, f"mb-{tid}", video, f"{title} (video)",
             "A Channel", topic, views, duration, state, float(tid)),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def library(tmp_path):
    db = tmp_path / "library.db"
    build_library(db)
    with S.SampletteLibrary(db) as lib:
        yield lib


PLAYABLE = 6  # rows 1-5 and 8


# ---------------------------------------------------------------------------


class TestAvailability:
    def test_a_missing_library_raises_with_a_fix(self, tmp_path):
        with pytest.raises(S.SampletteUnavailable) as exc:
            S.SampletteLibrary(tmp_path / "absent.db")
        assert "samplette-local" in str(exc.value)

    def test_available_reports_without_opening(self, tmp_path):
        assert not S.SampletteLibrary.available(tmp_path / "absent.db")
        build_library(tmp_path / "library.db")
        assert S.SampletteLibrary.available(tmp_path / "library.db")

    def test_the_library_is_opened_read_only(self, library):
        # NEYTA never crawls. samplette-local stays the only writer, so a bug
        # here cannot damage a database that took an evening to build.
        with pytest.raises(sqlite3.OperationalError):
            library._conn.execute("DELETE FROM tracks")


class TestPlayability:
    def test_only_resolved_tracks_are_returned(self, library):
        assert library.count() == PLAYABLE

    def test_pending_and_failed_rows_never_surface(self, library):
        ids = {t.id for t in library.sample(50)}
        assert 6 not in ids and 7 not in ids

    def test_every_returned_track_has_a_video(self, library):
        assert all(t.video_id for t in library.sample(50))

    def test_stats_report_the_unflattering_number_too(self, library):
        s = library.stats()
        assert s.total == len(ROWS)
        assert s.ready == PLAYABLE
        assert s.pending == 1
        assert s.with_key_and_tempo == 4
        assert 0 < s.ready_fraction < 1

    def test_ready_fraction_of_an_empty_library_is_zero_not_an_error(self, tmp_path):
        db = tmp_path / "empty.db"
        sqlite3.connect(db).executescript(SCHEMA)
        with S.SampletteLibrary(db) as lib:
            assert lib.stats().ready_fraction == 0.0


class TestFilters:
    def test_no_filters_matches_everything_playable(self, library):
        assert library.count(S.Filters()) == PLAYABLE

    def test_style_filter(self, library):
        assert library.count(S.Filters(styles=S.TagFilter.of("Bossa Nova"))) == 1

    def test_style_matching_is_by_whole_token(self, library):
        # "Samba" must not also match a hypothetical "Sambass".
        assert library.count(S.Filters(styles=S.TagFilter.of("Samba"))) == 1

    def test_two_styles_are_an_or_by_default(self, library):
        f = S.Filters(styles=S.TagFilter.of("Bossa Nova", "Country"))
        assert library.count(f) == 2

    def test_match_all_is_an_and(self, library):
        both = S.Filters(styles=S.TagFilter.of("Samba", "MPB", match_all=True))
        assert library.count(both) == 1
        neither = S.Filters(styles=S.TagFilter.of("Samba", "Country", match_all=True))
        assert library.count(neither) == 0

    def test_exclude_removes_matches(self, library):
        f = S.Filters(styles=S.TagFilter.of("Bossa Nova", exclude=True))
        assert library.count(f) == PLAYABLE - 1

    def test_exclude_keeps_untagged_rows(self, library):
        # NULL never satisfies LIKE, so a naive NOT would drop the untagged
        # row as well as the matching one.
        ids = {t.id for t in library.sample(50, S.Filters(
            styles=S.TagFilter.of("Bossa Nova", exclude=True)))}
        assert 8 in ids

    def test_region_filter(self, library):
        assert library.count(S.Filters(regions=S.TagFilter.of("Brazil"))) == 3

    def test_region_exclude_keeps_rows_with_no_region(self, library):
        ids = {t.id for t in library.sample(50, S.Filters(
            regions=S.TagFilter.of("Brazil", exclude=True)))}
        assert 8 in ids

    def test_key_filter(self, library):
        assert library.count(S.Filters(keys=S.TagFilter.of("E minor"))) == 1

    def test_tempo_range(self, library):
        assert library.count(S.Filters(tempo=S.Range(90, 110))) == 2

    def test_tempo_open_ended(self, library):
        assert library.count(S.Filters(tempo=S.Range(low=140))) == 1
        assert library.count(S.Filters(tempo=S.Range(high=100))) == 1

    def test_year_range(self, library):
        assert library.count(S.Filters(year=S.Range(1970, 1979))) == 2

    def test_duration_range(self, library):
        # rows 4 (160s), 2 (180s) and 3 (200s)
        assert library.count(S.Filters(duration=S.Range(150, 210))) == 3
        assert library.count(S.Filters(duration=S.Range(high=130))) == 2

    def test_topic_only(self, library):
        assert library.count(S.Filters(topic_only=True)) == 2

    def test_text_query_spans_artist_title_and_release(self, library):
        assert library.count(S.Filters(query="Jobim")) == 1
        assert library.count(S.Filters(query="Pau De Arara")) == 1

    def test_filters_combine(self, library):
        f = S.Filters(regions=S.TagFilter.of("Brazil"), year=S.Range(1970, 1979),
                      tempo=S.Range(90, 110))
        assert library.count(f) == 2

    def test_an_impossible_combination_matches_nothing(self, library):
        f = S.Filters(regions=S.TagFilter.of("Brazil"),
                      styles=S.TagFilter.of("Grunge"))
        assert library.count(f) == 0
        assert library.shuffle(f) is None

    def test_empty_tag_values_are_ignored(self, library):
        assert library.count(S.Filters(styles=S.TagFilter.of("", "  "))) == PLAYABLE

    def test_filters_are_falsy_when_empty(self):
        assert not S.Filters()
        assert S.Filters(query="x")
        assert S.Filters(tempo=S.Range(90, None))
        assert not S.Range()


class TestSqlSafety:
    @pytest.mark.parametrize(
        "hostile",
        ["'; DROP TABLE tracks; --", '" OR 1=1 --', "100' UNION SELECT"],
    )
    def test_hostile_text_is_bound_not_interpolated(self, library, hostile):
        # Filter values come from a text box and from Discogs metadata.
        assert library.count(S.Filters(query=hostile)) == 0
        assert library.count(S.Filters(styles=S.TagFilter.of(hostile))) == 0
        assert library.count() == PLAYABLE, "the table is still there"

    def test_an_unknown_facet_raises_rather_than_reaching_sql(self, library):
        with pytest.raises(ValueError):
            library.facet("'; DROP TABLE tracks; --")

    def test_an_unknown_range_raises(self, library):
        with pytest.raises(ValueError):
            library.bounds("nonsense")

    def test_an_unknown_mode_raises(self, library):
        with pytest.raises(ValueError):
            library.sample(1, mode="'; DROP TABLE tracks; --")


class TestModes:
    def test_popular_is_ordered_by_views(self, library):
        tracks = library.sample(3, mode="popular")
        assert [t.id for t in tracks] == [4, 1, 3]

    def test_recent_is_ordered_by_when_it_was_added(self, library):
        assert library.sample(2, mode="recent")[0].id == 8

    def test_shuffle_returns_something_playable(self, library):
        assert library.shuffle().video_id

    def test_shuffle_varies(self, library):
        seen = {library.shuffle().id for _ in range(40)}
        assert len(seen) > 1, "shuffle returned the same track 40 times"

    def test_sample_respects_its_limit(self, library):
        assert len(library.sample(3)) == 3

    def test_sample_of_more_than_exists_returns_what_exists(self, library):
        assert len(library.sample(999)) == PLAYABLE


class TestTrackMapping:
    def test_json_columns_become_tuples(self, library):
        track = library.get(3)
        assert track.styles == ("Samba", "MPB")
        assert track.genres == ("Latin",)

    def test_untagged_columns_become_empty_tuples(self, library):
        assert library.get(8).styles == ()

    def test_malformed_json_does_not_crash(self):
        assert S._load_json_list("{not json") == ()
        assert S._load_json_list(None) == ()
        assert S._load_json_list('"a string not a list"') == ()

    def test_url_is_a_watch_url(self, library):
        assert library.get(1).url == "https://www.youtube.com/watch?v=vid1"

    def test_get_of_a_missing_id_is_none(self, library):
        assert library.get(99999) is None

    def test_summary_reads_like_a_crate_label(self, library):
        summary = library.get(1).summary
        assert "Jobim" in summary and "1959" in summary
        assert "Brazil" in summary and "144 BPM" in summary

    def test_summary_survives_missing_metadata(self, library):
        assert library.get(8).summary


class TestToResult:
    def test_a_shuffled_track_is_an_ordinary_youtube_result(self, library):
        result = library.get(1).to_result()
        assert result.provider == "youtube"
        assert result.url.startswith("https://www.youtube.com/watch?v=")
        assert result.id == "vid1"

    def test_the_discogs_credit_wins_over_the_uploader_title(self, library):
        # So a file dragged into Ableton is named by the record, not by
        # whatever the uploader typed.
        result = library.get(1).to_result()
        assert result.artist == "Jobim"
        assert result.title == "Chega De Saudade"
        assert "(video)" not in result.title

    def test_bitrate_is_unknown_until_probed(self, library):
        # Same as any other YouTube result — the ladder is not known yet.
        assert library.get(1).to_result().source_kbps is None

    def test_crate_metadata_rides_along_in_extra(self, library):
        extra = library.get(1).to_result().extra
        assert extra["musical_key"] == "D major"
        assert extra["tempo"] == 144.0
        assert extra["region"] == "Brazil"
        assert extra["discogs_release_id"] == 1001

    def test_a_result_with_no_title_falls_back_rather_than_being_blank(self, library):
        track = library.get(8)
        assert track.to_result().title


class TestFacets:
    def test_style_facet_counts_only_playable_rows(self, library):
        facets = dict(library.facet("styles"))
        assert facets["MPB"] == 2
        assert "Free Jazz" not in facets, "row 6 is pending and must not appear"

    def test_region_facet(self, library):
        assert dict(library.facet("regions"))["Brazil"] == 3

    def test_facets_are_ordered_by_frequency(self, library):
        counts = [n for _, n in library.facet("styles")]
        assert counts == sorted(counts, reverse=True)

    def test_facet_limit(self, library):
        assert len(library.facet("styles", limit=2)) == 2

    def test_bounds(self, library):
        assert library.bounds("tempo") == (98.0, 144.0)
        assert library.bounds("year") == (1959, 2001)


class TestTaste:
    def test_an_empty_seed_set_has_no_profile(self, library):
        assert library.taste_profile([]) == {}

    def test_a_profile_weights_what_the_seeds_share(self, library):
        profile = library.taste_profile([library.get(2), library.get(3)])
        assert profile["style:MPB"] == 1.0
        assert profile["region:Brazil"] == 1.0
        assert "style:Country" not in profile

    def test_scoring_prefers_matching_tracks(self, library):
        profile = library.taste_profile([library.get(2), library.get(3)])
        assert library.score(profile, library.get(3)) > library.score(
            profile, library.get(4)
        )

    def test_score_without_a_profile_is_zero(self, library):
        assert library.score({}, library.get(1)) == 0.0

    def test_for_you_without_seeds_still_returns_something(self, library):
        assert library.for_you([], n=1)

    def test_for_you_does_not_return_a_seed_back(self, library):
        seeds = [library.get(2), library.get(3)]
        assert library.for_you(seeds, n=3)[0].id not in {2, 3}


# ---------------------------------------------------------------------------
# The real library
# ---------------------------------------------------------------------------

real_library = pytest.mark.skipif(
    not config.SAMPLETTE_DB.exists(),
    reason="no samplette-local library on this machine",
)


@pytest.mark.env
class TestRealLibrary:
    @pytest.fixture
    def real(self):
        with S.SampletteLibrary() as lib:
            yield lib

    @real_library
    def test_it_opens_while_samplette_local_may_be_writing(self, real):
        assert real.stats().total > 0

    @real_library
    def test_the_schema_still_matches_what_this_module_expects(self, real):
        # If samplette-local changes its schema, this is what says so.
        track = real.shuffle()
        assert track is not None
        assert track.video_id

    @real_library
    def test_shuffled_tracks_map_onto_the_youtube_provider(self, real):
        result = real.shuffle().to_result()
        assert result.provider == "youtube"
        assert "youtube.com/watch?v=" in result.url

    @real_library
    def test_the_playable_count_is_a_fraction_of_the_total(self, real):
        s = real.stats()
        assert s.ready < s.total
        assert s.pending > 0
