"""Background workers that build the local catalog.

Pipeline, each stage a daemon thread feeding the next through SQLite:

    seeds -> Discogs search -> release_queue
    release_queue -> Discogs release -> tracks (resolve_state='pending')
    pending tracks -> YouTube search -> tracks (resolve_state='ready')
    ready tracks -> MusicBrainz + AcousticBrainz -> key / tempo

Only the third stage gates playback, so the app is usable within seconds of
the first release being expanded.
"""
import json
import random
import threading
import time
from typing import Any, Dict, List, Optional

from . import db
from .config import (
    CRAWL_BATCH_RELEASES,
    DEFAULT_SEED_DECADES,
    DEFAULT_SEED_GENRES,
    DEFAULT_SEED_STYLES,
    READY_BUFFER_TARGET,
)
from .sources import acousticbrainz, discogs, youtube

# Discogs search pagination stops being useful well before the reported end.
MAX_SEED_PAGE = 40
MAX_RESOLVE_ATTEMPTS = 2
MAX_RELEASE_ATTEMPTS = 2
# Total attempts before a track is written off for good, across revivals.
RETRY_ATTEMPT_CEILING = 6


class Crawler:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self.status: Dict[str, Any] = {
            "running": False,
            "stage": "idle",
            "last_error": None,
            "resolved_session": 0,
            "enriched_session": 0,
        }
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- control

    def start(self) -> None:
        if self.status["running"]:
            return
        self._stop.clear()
        self.status["running"] = True
        targets = (
            ("seeder", self._seed_loop),
            ("expander", self._expand_loop),
            ("resolver", self._resolve_loop),
            ("enricher", self._enrich_loop),
        )
        for name, fn in targets:
            t = threading.Thread(target=self._guard(fn), name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        self.status["running"] = False

    def _guard(self, fn):
        """Keep a worker alive across unexpected errors."""
        def wrapper():
            while not self._stop.is_set():
                try:
                    fn()
                    return
                except Exception as exc:  # noqa: BLE001 - workers must not die
                    with self._lock:
                        self.status["last_error"] = "{}: {}".format(
                            type(exc).__name__, exc)
                    self._stop.wait(10)
        return wrapper

    def _sleep(self, secs: float) -> bool:
        """Interruptible sleep. Returns True if we should keep running."""
        return not self._stop.wait(secs)

    # ------------------------------------------------------------- stage 1/4

    def _custom_seeds(self) -> Optional[List[Dict[str, Any]]]:
        raw = db.get_setting("seed_config")
        if not raw:
            return None
        try:
            seeds = json.loads(raw)
            return seeds if isinstance(seeds, list) and seeds else None
        except ValueError:
            return None

    def _pick_seed(self) -> Dict[str, Any]:
        """Choose a facet to dig through next, preferring unexhausted ones."""
        custom = self._custom_seeds()
        if custom:
            choice = random.choice(custom)
        else:
            choice = {}
            roll = random.random()
            if roll < 0.55:
                choice["style"] = random.choice(DEFAULT_SEED_STYLES)
            else:
                choice["genre"] = random.choice(DEFAULT_SEED_GENRES)
            if random.random() < 0.7:
                decade = random.choice(DEFAULT_SEED_DECADES)
                choice["year_from"] = decade
                choice["year_to"] = decade + 9

        key = json.dumps(choice, sort_keys=True)
        row = db.q1("SELECT next_page, exhausted FROM seeds WHERE key=?", (key,))
        if row is None:
            db.run(
                "INSERT OR IGNORE INTO seeds (key, next_page, last_run) "
                "VALUES (?,1,?)", (key, time.time()))
            page = 1
        elif row["exhausted"]:
            page = random.randint(1, MAX_SEED_PAGE)  # revisit with fresh offset
        else:
            page = int(row["next_page"])
        choice["_key"] = key
        choice["_page"] = min(page, MAX_SEED_PAGE)
        return choice

    def _seed_loop(self) -> None:
        while self._sleep(0):
            if self._backlog_full():
                if not self._sleep(20):
                    return
                continue

            seed = self._pick_seed()
            self._set_stage("searching Discogs")
            result = discogs.search_releases(
                genre=seed.get("genre"),
                style=seed.get("style"),
                year_from=seed.get("year_from"),
                year_to=seed.get("year_to"),
                country=seed.get("country"),
                query=seed.get("query"),
                page=seed["_page"],
                per_page=CRAWL_BATCH_RELEASES * 2,
            )

            now = time.time()
            for rid in result["ids"]:
                db.run(
                    "INSERT OR IGNORE INTO release_queue "
                    "(discogs_release_id, queued_at) VALUES (?,?)", (rid, now))

            next_page = seed["_page"] + 1
            exhausted = 1 if (not result["ids"]
                              or next_page > min(MAX_SEED_PAGE,
                                                 result["pages"] or 1)) else 0
            db.run(
                "INSERT INTO seeds (key, next_page, exhausted, last_run) "
                "VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                "next_page=excluded.next_page, exhausted=excluded.exhausted, "
                "last_run=excluded.last_run",
                (seed["_key"], next_page, exhausted, now))

            if not self._sleep(2):
                return

    def _backlog_full(self) -> bool:
        """Stop fetching new releases while there's plenty already queued."""
        row = db.q1("SELECT COUNT(*) AS n FROM release_queue WHERE state='pending'")
        return bool(row and row["n"] >= 300)

    # ------------------------------------------------------------- stage 2/4

    def _expand_loop(self) -> None:
        while self._sleep(0):
            # Random rather than FIFO: releases arrive grouped by search facet,
            # so draining the queue in order would give you an hour of one
            # style before anything else turned up.
            row = db.q1(
                "SELECT discogs_release_id FROM release_queue "
                "WHERE state='pending' ORDER BY RANDOM() LIMIT 1")
            if not row:
                if not self._sleep(5):
                    return
                continue

            rid = int(row["discogs_release_id"])
            db.run("UPDATE release_queue SET state='working', attempts=attempts+1 "
                   "WHERE discogs_release_id=?", (rid,))
            self._set_stage("expanding release {}".format(rid))

            try:
                tracks = discogs.get_release_tracks(rid)
            except Exception:
                tracks = []

            if tracks:
                for t in tracks:
                    db.upsert_track(t)
                db.run("UPDATE release_queue SET state='done' "
                       "WHERE discogs_release_id=?", (rid,))
            else:
                att = db.q1("SELECT attempts FROM release_queue "
                            "WHERE discogs_release_id=?", (rid,))
                state = ("failed" if att and att["attempts"] >= MAX_RELEASE_ATTEMPTS
                         else "pending")
                db.run("UPDATE release_queue SET state=? "
                       "WHERE discogs_release_id=?", (state, rid))

            if not self._sleep(1):
                return

    # ------------------------------------------------------------- stage 3/4

    def _resolve_loop(self) -> None:
        while self._sleep(0):
            # Resolved tracks are never consumed, so the buffer target throttles
            # rather than stops: below it we sprint to get you listening fast,
            # above it we keep going gently so the library grows all session
            # instead of freezing at the target forever.
            ready = db.q1(
                "SELECT COUNT(*) AS n FROM tracks WHERE resolve_state='ready'")
            warmed_up = bool(ready and ready["n"] >= READY_BUFFER_TARGET)
            pace = 6.0 if warmed_up else 0.5

            row = db.q1(
                "SELECT id, artist, title FROM tracks WHERE resolve_state='pending' "
                "ORDER BY RANDOM() LIMIT 1")
            if not row:
                # Nothing queued. Give previously failed tracks another go:
                # most failures are transient (dropped network, YouTube
                # throttling), and without this a bad spell would permanently
                # burn part of the catalog. Genuinely unfindable tracks stop
                # at RETRY_ATTEMPT_CEILING.
                self._revive_failed()
                if not self._sleep(4):
                    return
                continue

            self.resolve_track(int(row["id"]), row["artist"], row["title"])
            if not self._sleep(pace):
                return

    def _revive_failed(self, batch: int = 50) -> int:
        """Return a batch of failed tracks to the queue for another attempt."""
        cur = db.run(
            "UPDATE tracks SET resolve_state='pending' WHERE id IN ("
            "  SELECT id FROM tracks WHERE resolve_state='failed' "
            "  AND resolve_attempts < ? ORDER BY RANDOM() LIMIT ?)",
            (RETRY_ATTEMPT_CEILING, batch))
        return cur.rowcount or 0

    def resolve_track(self, track_id: int, artist: str, title: str) -> bool:
        """Attach a YouTube video to one track. Safe to call from a request."""
        db.run("UPDATE tracks SET resolve_state='resolving', "
               "resolve_attempts=resolve_attempts+1 WHERE id=?", (track_id,))
        self._set_stage("resolving {} - {}".format(artist, title))

        try:
            hit = youtube.find_for_track(artist, title)
        except Exception:
            hit = None

        if not hit:
            row = db.q1("SELECT resolve_attempts FROM tracks WHERE id=?",
                        (track_id,))
            attempts = int(row["resolve_attempts"]) if row else 99
            state = "failed" if attempts >= MAX_RESOLVE_ATTEMPTS else "pending"
            db.run("UPDATE tracks SET resolve_state=? WHERE id=?",
                   (state, track_id))
            return False

        db.run(
            """UPDATE tracks SET yt_video_id=?, yt_title=?, yt_channel=?,
               yt_channel_id=?, yt_is_topic=?, yt_views=?, yt_duration=?,
               resolve_state='ready' WHERE id=?""",
            (hit["yt_video_id"], hit["yt_title"], hit["yt_channel"],
             hit["yt_channel_id"], hit["yt_is_topic"], hit["yt_views"],
             hit["yt_duration"], track_id))
        with self._lock:
            self.status["resolved_session"] += 1
        return True

    # ------------------------------------------------------------- stage 4/4

    def _enrich_loop(self) -> None:
        while self._sleep(0):
            row = db.q1(
                "SELECT id, artist, title FROM tracks "
                "WHERE resolve_state='ready' AND enrich_state=0 "
                "ORDER BY RANDOM() LIMIT 1")
            if not row:
                if not self._sleep(8):
                    return
                continue

            self.enrich_track(int(row["id"]), row["artist"], row["title"])
            if not self._sleep(1.2):
                return

    def enrich_track(self, track_id: int, artist: str, title: str) -> bool:
        """Fill in key/tempo. enrich_state 2 means 'looked, nothing there'."""
        try:
            data = acousticbrainz.enrich(artist, title)
        except Exception:
            data = None

        if not data:
            db.run("UPDATE tracks SET enrich_state=2 WHERE id=?", (track_id,))
            return False

        db.run(
            """UPDATE tracks SET mb_recording_id=?,
               musical_key=COALESCE(?, musical_key),
               tempo=COALESCE(?, tempo),
               enrich_state=? WHERE id=?""",
            (data.get("mb_recording_id"), data.get("musical_key"),
             data.get("tempo"),
             1 if ("musical_key" in data or "tempo" in data) else 2,
             track_id))
        if "musical_key" in data or "tempo" in data:
            with self._lock:
                self.status["enriched_session"] += 1
            return True
        return False

    # ----------------------------------------------------------------- misc

    def _set_stage(self, stage: str) -> None:
        with self._lock:
            self.status["stage"] = stage


crawler = Crawler()
