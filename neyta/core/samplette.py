"""Crate-digging over samplette-local's library.

samplette-local crawls Discogs -> YouTube -> MusicBrainz -> AcousticBrainz into
a SQLite file and serves it through its own Flask app. NEYTA reads that file
directly, read-only, and hands the resulting video straight to the existing
YouTube provider — a shuffled track is an ordinary result that flows into the
same format picker, stem picker and drag tray as anything you searched for.

NEYTA does not crawl. Keeping the writer in one process avoids two crawlers
racing on the same database, and it is why the Discogs token stays a
samplette-local setting rather than becoming a NEYTA one.

Only `resolve_state = 'ready'` rows have a YouTube video behind them. At the
time of writing that is 9,030 of 179,109 rows — the rest are queued for
resolution — so every query here filters on it and `stats()` reports both
numbers rather than the flattering one.
"""

from __future__ import annotations

import json
import random
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .. import config
from ..providers.base import Result


class SampletteUnavailable(RuntimeError):
    """No library file, or it is not readable."""


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TagFilter:
    """A set-membership filter over one column.

    `match_all` turns OR into AND (jazz *and* funk, not jazz *or* funk).
    `exclude` inverts the whole clause.
    """

    values: tuple[str, ...] = ()
    match_all: bool = False
    exclude: bool = False

    def __bool__(self) -> bool:
        return bool(self.values)

    @classmethod
    def of(cls, *values: str, match_all: bool = False, exclude: bool = False):
        return cls(tuple(v for v in values if v and v.strip()), match_all, exclude)


@dataclass(frozen=True)
class Range:
    low: float | None = None
    high: float | None = None

    def __bool__(self) -> bool:
        return self.low is not None or self.high is not None


@dataclass(frozen=True)
class Filters:
    """1970s Brazilian jazz-funk in F minor between 90 and 100 BPM, and so on."""

    query: str = ""
    genres: TagFilter = field(default_factory=TagFilter)
    styles: TagFilter = field(default_factory=TagFilter)
    tags: TagFilter = field(default_factory=TagFilter)
    regions: TagFilter = field(default_factory=TagFilter)
    keys: TagFilter = field(default_factory=TagFilter)
    tempo: Range = field(default_factory=Range)
    year: Range = field(default_factory=Range)
    views: Range = field(default_factory=Range)
    duration: Range = field(default_factory=Range)
    topic_only: bool = False

    def __bool__(self) -> bool:
        return any(
            [self.query.strip(), self.genres, self.styles, self.tags,
             self.regions, self.keys, self.tempo, self.year, self.views,
             self.duration, self.topic_only]
        )


#: JSON-array text columns, matched by their quoted token so that "Jazz" does
#: not also match "Jazz-Funk".
_JSON_COLUMNS = {"genres": "genres", "styles": "styles", "tags": "tags"}
#: Plain text columns matched by equality.
_TEXT_COLUMNS = {"regions": "region", "keys": "musical_key"}
#: Numeric columns matched by range.
_RANGE_COLUMNS = {
    "tempo": "tempo", "year": "year", "views": "yt_views", "duration": "yt_duration",
}


def _json_clause(column: str, f: TagFilter) -> tuple[str, list[Any]]:
    # The quotes come from the JSON encoding, so a value is matched as a whole
    # token. Stripping any embedded quote keeps the pattern well-formed.
    terms = [f"{column} LIKE ?" for _ in f.values]
    args = [f'%"{v.replace(chr(34), "")}"%' for v in f.values]
    joiner = " AND " if (f.match_all and not f.exclude) else " OR "
    clause = "(" + joiner.join(terms) + ")"
    if f.exclude:
        # NULL never satisfies LIKE, so an excluding filter must let untagged
        # rows through rather than silently dropping them.
        clause = f"({column} IS NULL OR NOT {clause})"
    return clause, args


def _text_clause(column: str, f: TagFilter) -> tuple[str, list[Any]]:
    placeholders = ",".join("?" for _ in f.values)
    if f.exclude:
        return f"({column} IS NULL OR {column} NOT IN ({placeholders}))", list(f.values)
    return f"{column} IN ({placeholders})", list(f.values)


def build_where(filters: Filters | None) -> tuple[str, list[Any]]:
    """(sql, args) restricting to playable rows that match.

    Column names come from the fixed maps above and values are always bound
    parameters, so nothing a user types reaches the SQL text.
    """
    clauses = ["resolve_state = 'ready'", "yt_video_id IS NOT NULL"]
    args: list[Any] = []
    if filters is None:
        return " AND ".join(clauses), args

    for name, column in _JSON_COLUMNS.items():
        if f := getattr(filters, name):
            clause, a = _json_clause(column, f)
            clauses.append(clause)
            args += a

    for name, column in _TEXT_COLUMNS.items():
        if f := getattr(filters, name):
            clause, a = _text_clause(column, f)
            clauses.append(clause)
            args += a

    for name, column in _RANGE_COLUMNS.items():
        r: Range = getattr(filters, name)
        if r.low is not None:
            clauses.append(f"{column} >= ?")
            args.append(r.low)
        if r.high is not None:
            clauses.append(f"{column} <= ?")
            args.append(r.high)

    if filters.topic_only:
        # "- Topic" channels are auto-generated by YouTube from the label's
        # own delivery, so they are the cleanest audio on the platform.
        clauses.append("yt_is_topic = 1")

    if text := filters.query.strip():
        clauses.append("(artist LIKE ? OR title LIKE ? OR release LIKE ?)")
        args += [f"%{text}%"] * 3

    return " AND ".join(clauses), args


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------


def _load_json_list(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except ValueError:
        return ()
    return tuple(str(v) for v in parsed) if isinstance(parsed, list) else ()


@dataclass(frozen=True)
class SampletteTrack:
    id: int
    artist: str | None
    title: str | None
    release: str | None
    year: int | None
    label: str | None
    region: str | None
    genres: tuple[str, ...]
    styles: tuple[str, ...]
    tags: tuple[str, ...]
    musical_key: str | None
    tempo: float | None
    video_id: str
    yt_title: str | None
    yt_channel: str | None
    yt_views: int | None
    yt_duration: int | None
    yt_is_topic: bool
    discogs_release_id: int | None
    mb_recording_id: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "SampletteTrack":
        return cls(
            id=row["id"],
            artist=row["artist"],
            title=row["title"],
            release=row["release"],
            year=row["year"],
            label=row["label"],
            region=row["region"],
            genres=_load_json_list(row["genres"]),
            styles=_load_json_list(row["styles"]),
            tags=_load_json_list(row["tags"]),
            musical_key=row["musical_key"],
            tempo=row["tempo"],
            video_id=row["yt_video_id"],
            yt_title=row["yt_title"],
            yt_channel=row["yt_channel"],
            yt_views=row["yt_views"],
            yt_duration=row["yt_duration"],
            yt_is_topic=bool(row["yt_is_topic"]),
            discogs_release_id=row["discogs_release_id"],
            mb_recording_id=row["mb_recording_id"],
        )

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"

    def to_result(self) -> Result:
        """A shuffled track is an ordinary YouTube result.

        The Discogs credit is preferred over the YouTube title, so a file
        dragged into Ableton is named "Antonio Carlos Jobim - Chega De Saudade"
        rather than whatever the uploader typed.
        """
        return Result(
            provider="youtube",
            id=self.video_id,
            title=self.title or self.yt_title or "untitled",
            artist=self.artist,
            duration=float(self.yt_duration) if self.yt_duration else None,
            url=self.url,
            source_kbps=None,  # unknown until probed, like any YouTube result
            extra={
                "samplette_id": self.id,
                "release": self.release,
                "year": self.year,
                "label": self.label,
                "region": self.region,
                "genres": list(self.genres),
                "styles": list(self.styles),
                "musical_key": self.musical_key,
                "tempo": self.tempo,
                "views": self.yt_views,
                "channel": self.yt_channel,
                "is_topic": self.yt_is_topic,
                "discogs_release_id": self.discogs_release_id,
                "mb_recording_id": self.mb_recording_id,
            },
        )

    @property
    def summary(self) -> str:
        bits = [b for b in (self.artist, self.title) if b]
        line = " — ".join(bits) or "untitled"
        meta = []
        if self.year:
            meta.append(str(self.year))
        if self.region:
            meta.append(self.region)
        if self.styles:
            meta.append("/".join(self.styles[:2]))
        if self.musical_key:
            meta.append(self.musical_key)
        if self.tempo:
            meta.append(f"{self.tempo:.0f} BPM")
        return f"{line}  ({', '.join(meta)})" if meta else line


# ---------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------

_COLUMNS = """
    id, artist, title, release, year, label, region, genres, styles, tags,
    musical_key, tempo, discogs_release_id, mb_recording_id, yt_video_id,
    yt_title, yt_channel, yt_views, yt_duration, yt_is_topic
"""

MODES = ("shuffle", "popular", "recent")


@dataclass
class LibraryStats:
    total: int
    ready: int
    pending: int
    with_key_and_tempo: int

    @property
    def ready_fraction(self) -> float:
        return self.ready / self.total if self.total else 0.0


class SampletteLibrary:
    """Read-only access to samplette-local's library.

    Opened `mode=ro` so that a crawl running in samplette-local is never
    disturbed, and so a bug here cannot damage a database that took an evening
    to build.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else config.SAMPLETTE_DB
        if not self.path.exists():
            raise SampletteUnavailable(
                f"no samplette library at {self.path}. Run samplette-local "
                "once to build one."
            )
        try:
            self._conn = sqlite3.connect(
                f"file:{self.path}?mode=ro", uri=True, check_same_thread=False
            )
        except sqlite3.Error as exc:
            raise SampletteUnavailable(f"cannot open {self.path}: {exc}") from exc
        self._conn.row_factory = sqlite3.Row
        self._rng = random.Random()

    @classmethod
    def available(cls, path: Path | str | None = None) -> bool:
        target = Path(path) if path is not None else config.SAMPLETTE_DB
        return target.exists()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SampletteLibrary":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- queries ---------------------------------------------------------

    def count(self, filters: Filters | None = None) -> int:
        where, args = build_where(filters)
        return int(
            self._conn.execute(
                f"SELECT COUNT(*) FROM tracks WHERE {where}", args
            ).fetchone()[0]
        )

    def stats(self) -> LibraryStats:
        row = self._conn.execute(
            "SELECT COUNT(*) total,"
            " SUM(resolve_state='ready' AND yt_video_id IS NOT NULL) ready,"
            " SUM(resolve_state='pending') pending,"
            " SUM(resolve_state='ready' AND musical_key IS NOT NULL"
            "     AND tempo IS NOT NULL) keyed"
            " FROM tracks"
        ).fetchone()
        return LibraryStats(
            total=row["total"] or 0,
            ready=row["ready"] or 0,
            pending=row["pending"] or 0,
            with_key_and_tempo=row["keyed"] or 0,
        )

    def shuffle(
        self, filters: Filters | None = None, *, mode: str = "shuffle"
    ) -> SampletteTrack | None:
        """One track, or None when nothing matches.

        None is a real answer here — a tight filter over 9,030 playable tracks
        will often match nothing, and the UI should say so rather than
        silently widening what was asked for.
        """
        tracks = self.sample(1, filters, mode=mode)
        return tracks[0] if tracks else None

    def sample(
        self, n: int, filters: Filters | None = None, *, mode: str = "shuffle"
    ) -> list[SampletteTrack]:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}; known: {', '.join(MODES)}")
        where, args = build_where(filters)
        order = {
            "shuffle": "RANDOM()",
            "popular": "yt_views DESC",
            "recent": "added_at DESC",
        }[mode]
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM tracks WHERE {where} "
            f"ORDER BY {order} LIMIT ?",
            [*args, max(1, n)],
        ).fetchall()
        return [SampletteTrack.from_row(r) for r in rows]

    def get(self, track_id: int) -> SampletteTrack | None:
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM tracks WHERE id = ?", (track_id,)
        ).fetchone()
        return SampletteTrack.from_row(row) if row else None

    # -- facets ----------------------------------------------------------

    def facet(self, name: str, limit: int = 200) -> list[tuple[str, int]]:
        """Distinct values and their counts, for populating the filter panel.

        Only counts playable rows, so the panel never offers a filter that can
        only ever return nothing.
        """
        where, args = build_where(None)

        if name in _JSON_COLUMNS:
            column = _JSON_COLUMNS[name]
            counts: dict[str, int] = {}
            for (raw,) in self._conn.execute(
                f"SELECT {column} FROM tracks WHERE {where} AND {column} IS NOT NULL",
                args,
            ):
                for value in _load_json_list(raw):
                    counts[value] = counts.get(value, 0) + 1
            ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
            return ordered[:limit]

        if name in _TEXT_COLUMNS:
            column = _TEXT_COLUMNS[name]
            rows = self._conn.execute(
                f"SELECT {column} v, COUNT(*) n FROM tracks "
                f"WHERE {where} AND {column} IS NOT NULL "
                f"GROUP BY v ORDER BY n DESC, v LIMIT ?",
                [*args, limit],
            ).fetchall()
            return [(r["v"], r["n"]) for r in rows]

        raise ValueError(f"no facet named {name!r}")

    def bounds(self, name: str) -> tuple[float | None, float | None]:
        """(min, max) of a numeric column over playable rows — for slider ends."""
        if name not in _RANGE_COLUMNS:
            raise ValueError(f"no range named {name!r}")
        column = _RANGE_COLUMNS[name]
        where, args = build_where(None)
        row = self._conn.execute(
            f"SELECT MIN({column}) lo, MAX({column}) hi FROM tracks WHERE {where}",
            args,
        ).fetchone()
        return row["lo"], row["hi"]

    # -- taste -----------------------------------------------------------

    def taste_profile(self, tracks: Iterable[SampletteTrack]) -> dict[str, float]:
        """Weights over styles and regions, from tracks you kept.

        Feeds the "for you" mode: not a recommender, just a tally of what the
        seeds have in common, normalised so one prolific style does not swamp
        the rest.
        """
        counts: dict[str, float] = {}
        n = 0
        for track in tracks:
            n += 1
            for value in (*track.styles, *track.genres):
                counts[f"style:{value}"] = counts.get(f"style:{value}", 0.0) + 1.0
            if track.region:
                counts[f"region:{track.region}"] = (
                    counts.get(f"region:{track.region}", 0.0) + 1.0
                )
        if not n:
            return {}
        return {k: v / n for k, v in counts.items()}

    def score(self, profile: dict[str, float], track: SampletteTrack) -> float:
        if not profile:
            return 0.0
        total = 0.0
        for value in (*track.styles, *track.genres):
            total += profile.get(f"style:{value}", 0.0)
        if track.region:
            total += profile.get(f"region:{track.region}", 0.0)
        return total

    def for_you(
        self, seeds: Sequence[SampletteTrack], *, pool: int = 400, n: int = 1
    ) -> list[SampletteTrack]:
        """Sample a pool at random, then rank it against the seed profile.

        Ranking a random pool rather than the whole library keeps the result
        surprising — which is the entire point of a crate-digging tool — while
        still leaning toward what you liked.
        """
        profile = self.taste_profile(seeds)
        candidates = self.sample(pool)
        if not profile:
            return candidates[:n]
        seen = {s.id for s in seeds}
        ranked = sorted(
            (c for c in candidates if c.id not in seen),
            key=lambda c: self.score(profile, c),
            reverse=True,
        )
        return ranked[:n]
