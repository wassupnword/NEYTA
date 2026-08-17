"""SQLite library: the local stand-in for Samplette's server-side catalog.

One file at data/library.db holds the crawled catalog plus everything that was
account-bound on the website (playlists, favorites, history, notes).
"""
import json
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

from .config import DB_PATH

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    artist              TEXT NOT NULL,
    title               TEXT NOT NULL,
    release             TEXT,
    year                INTEGER,
    label               TEXT,
    region              TEXT,
    copyright           TEXT,
    p_copyright         TEXT,
    genres              TEXT DEFAULT '[]',
    styles              TEXT DEFAULT '[]',
    tags                TEXT DEFAULT '[]',
    musical_key         TEXT,
    tempo               REAL,
    discogs_release_id  INTEGER,
    mb_recording_id     TEXT,
    yt_video_id         TEXT,
    yt_title            TEXT,
    yt_channel          TEXT,
    yt_channel_id       TEXT,
    yt_is_topic         INTEGER DEFAULT 0,
    yt_views            INTEGER,
    yt_duration         INTEGER,
    -- pending -> resolving -> ready | failed
    resolve_state       TEXT DEFAULT 'pending',
    resolve_attempts    INTEGER DEFAULT 0,
    -- 0 = not tried, 1 = done, 2 = no data available
    enrich_state        INTEGER DEFAULT 0,
    added_at            REAL,
    UNIQUE (artist, title, release)
);
CREATE INDEX IF NOT EXISTS idx_tracks_state   ON tracks (resolve_state);
CREATE INDEX IF NOT EXISTS idx_tracks_enrich  ON tracks (enrich_state, resolve_state);
CREATE INDEX IF NOT EXISTS idx_tracks_year    ON tracks (year);
CREATE INDEX IF NOT EXISTS idx_tracks_views   ON tracks (yt_views);
CREATE INDEX IF NOT EXISTS idx_tracks_artist  ON tracks (artist);
CREATE INDEX IF NOT EXISTS idx_tracks_video   ON tracks (yt_video_id);

-- Discogs releases queued for tracklist expansion.
CREATE TABLE IF NOT EXISTS release_queue (
    discogs_release_id  INTEGER PRIMARY KEY,
    state               TEXT DEFAULT 'pending',
    attempts            INTEGER DEFAULT 0,
    queued_at           REAL
);
CREATE INDEX IF NOT EXISTS idx_relq_state ON release_queue (state);

-- Search facets already consumed, so the crawler advances instead of looping.
CREATE TABLE IF NOT EXISTS seeds (
    key         TEXT PRIMARY KEY,
    next_page   INTEGER DEFAULT 1,
    exhausted   INTEGER DEFAULT 0,
    last_run    REAL
);

CREATE TABLE IF NOT EXISTS playlists (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    created_at  REAL
);

CREATE TABLE IF NOT EXISTS playlist_tracks (
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    track_id    INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    added_at    REAL,
    PRIMARY KEY (playlist_id, track_id)
);

CREATE TABLE IF NOT EXISTS favorites (
    track_id    INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
    added_at    REAL
);

CREATE TABLE IF NOT EXISTS history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id    INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    played_at   REAL
);
CREATE INDEX IF NOT EXISTS idx_history_time ON history (played_at DESC);

CREATE TABLE IF NOT EXISTS notes (
    track_id    INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
    body        TEXT,
    updated_at  REAL
);

CREATE TABLE IF NOT EXISTS settings (
    key     TEXT PRIMARY KEY,
    value   TEXT
);
"""


def conn() -> sqlite3.Connection:
    """One connection per thread; SQLite objects aren't shareable across them."""
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(str(DB_PATH), timeout=30.0)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA foreign_keys=ON")
        _local.conn = c
    return c


def init() -> None:
    c = conn()
    c.executescript(SCHEMA)
    c.commit()


def q(sql: str, args: tuple = ()) -> List[sqlite3.Row]:
    return conn().execute(sql, args).fetchall()


def q1(sql: str, args: tuple = ()) -> Optional[sqlite3.Row]:
    return conn().execute(sql, args).fetchone()


def run(sql: str, args: tuple = ()) -> sqlite3.Cursor:
    c = conn()
    cur = c.execute(sql, args)
    c.commit()
    return cur


def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    row = q1("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    run(
        "INSERT INTO settings (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def _jl(raw: Any) -> List[str]:
    """Parse a JSON list column, tolerating nulls and legacy plain strings."""
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        val = json.loads(raw)
        return val if isinstance(val, list) else []
    except (ValueError, TypeError):
        return []


def track_dict(row: sqlite3.Row) -> Dict[str, Any]:
    """Row -> the JSON shape the frontend renders."""
    d = dict(row)
    for key in ("genres", "styles", "tags"):
        d[key] = _jl(d.get(key))
    d["duration_str"] = _fmt_duration(d.get("yt_duration"))
    d["views_str"] = _fmt_views(d.get("yt_views"))
    d["tempo_str"] = "{:.0f} BPM".format(d["tempo"]) if d.get("tempo") else None
    return d


def _fmt_duration(secs: Optional[float]) -> Optional[str]:
    if not secs:
        return None
    secs = int(secs)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "{}:{:02d}:{:02d}".format(h, m, s)
    return "{}:{:02d}".format(m, s)


def _fmt_views(v: Optional[int]) -> Optional[str]:
    if v is None:
        return None
    if v >= 1_000_000:
        return "{:.1f}M views".format(v / 1_000_000).replace(".0M", "M")
    if v >= 1_000:
        return "{:.1f}K views".format(v / 1_000).replace(".0K", "K")
    return "{} views".format(v)


def upsert_track(t: Dict[str, Any]) -> Optional[int]:
    """Insert a crawled track. Returns its id, or None if already present."""
    cur = run(
        """INSERT OR IGNORE INTO tracks
           (artist, title, release, year, label, region, copyright, p_copyright,
            genres, styles, tags, discogs_release_id, added_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            t["artist"], t["title"], t.get("release"), t.get("year"),
            t.get("label"), t.get("region"), t.get("copyright"),
            t.get("p_copyright"),
            json.dumps(t.get("genres") or []),
            json.dumps(t.get("styles") or []),
            json.dumps(t.get("tags") or []),
            t.get("discogs_release_id"), time.time(),
        ),
    )
    return cur.lastrowid if cur.rowcount else None


def stats() -> Dict[str, int]:
    def n(sql: str, args: tuple = ()) -> int:
        row = q1(sql, args)
        return int(row[0]) if row else 0

    return {
        "total": n("SELECT COUNT(*) FROM tracks"),
        "ready": n("SELECT COUNT(*) FROM tracks WHERE resolve_state='ready'"),
        "pending": n("SELECT COUNT(*) FROM tracks WHERE resolve_state='pending'"),
        "failed": n("SELECT COUNT(*) FROM tracks WHERE resolve_state='failed'"),
        "with_key": n(
            "SELECT COUNT(*) FROM tracks WHERE musical_key IS NOT NULL"),
        "releases_pending": n(
            "SELECT COUNT(*) FROM release_queue WHERE state='pending'"),
        "favorites": n("SELECT COUNT(*) FROM favorites"),
        "played": n("SELECT COUNT(*) FROM history"),
    }
