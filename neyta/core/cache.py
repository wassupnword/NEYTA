"""sqlite cache for captions, searches and probes.

This is what keeps phrase search under YouTube's rate limit in normal use
(build plan 5.3). Caption data for a published video does not change, so it is
stored without expiry; searches and probes do change, so they carry a TTL.

Accessed from worker threads, so every statement runs under one lock.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import config

CAPTIONS = "captions"
SEARCH = "search"
PROBE = "probe"

#: Seconds, or None for "never expires".
DEFAULT_TTL: dict[str, float | None] = {
    CAPTIONS: config.CAPTION_TTL_SECONDS,
    SEARCH: config.SEARCH_TTL_SECONDS,
    PROBE: config.PROBE_TTL_SECONDS,
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    namespace  TEXT NOT NULL,
    key        TEXT NOT NULL,
    payload    TEXT NOT NULL,
    stored_at  REAL NOT NULL,
    PRIMARY KEY (namespace, key)
);
CREATE INDEX IF NOT EXISTS entries_ns_time ON entries (namespace, stored_at);
"""

_MISSING = object()


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    expirations: int = 0
    writes: int = 0

    @property
    def lookups(self) -> int:
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        return self.hits / self.lookups if self.lookups else 0.0


class Cache:
    """Namespaced key/value store with per-namespace TTL.

    An expired entry counts as a miss *and* an expiration, and is deleted on
    read so the file does not grow without bound between explicit purges.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self._lock = threading.RLock()
        self.stats = CacheStats()

        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            target = str(self.path)
        else:
            target = ":memory:"

        self._conn = sqlite3.connect(target, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            if self.path is not None:
                # Survives a hard quit mid-write, and lets a reader and the
                # writer coexist without the UI stalling on a download.
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # -- generic ---------------------------------------------------------

    def get(
        self,
        namespace: str,
        key: str,
        *,
        ttl: float | None | object = _MISSING,
        now: float | None = None,
    ) -> Any | None:
        """The stored value, or None on miss or expiry.

        `ttl` overrides the namespace default; pass None for "never expires".
        `now` is injectable so expiry can be tested without sleeping.
        """
        effective_ttl = DEFAULT_TTL.get(namespace) if ttl is _MISSING else ttl
        now = time.time() if now is None else now

        with self._lock:
            row = self._conn.execute(
                "SELECT payload, stored_at FROM entries WHERE namespace=? AND key=?",
                (namespace, key),
            ).fetchone()

            if row is None:
                self.stats.misses += 1
                return None

            if effective_ttl is not None and now - row["stored_at"] > effective_ttl:
                self._conn.execute(
                    "DELETE FROM entries WHERE namespace=? AND key=?", (namespace, key)
                )
                self._conn.commit()
                self.stats.misses += 1
                self.stats.expirations += 1
                return None

            self.stats.hits += 1
            return json.loads(row["payload"])

    def put(
        self, namespace: str, key: str, value: Any, *, now: float | None = None
    ) -> None:
        payload = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        now = time.time() if now is None else now
        with self._lock:
            self._conn.execute(
                "INSERT INTO entries (namespace, key, payload, stored_at) "
                "VALUES (?,?,?,?) "
                "ON CONFLICT(namespace, key) DO UPDATE SET "
                "payload=excluded.payload, stored_at=excluded.stored_at",
                (namespace, key, payload, now),
            )
            self._conn.commit()
            self.stats.writes += 1

    def delete(self, namespace: str, key: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM entries WHERE namespace=? AND key=?", (namespace, key)
            )
            self._conn.commit()
            return cur.rowcount > 0

    def purge(self, namespace: str | None = None) -> int:
        """Drop a namespace, or everything. Part of the global wipe in the
        settings dialog (build plan 4)."""
        with self._lock:
            if namespace is None:
                cur = self._conn.execute("DELETE FROM entries")
            else:
                cur = self._conn.execute(
                    "DELETE FROM entries WHERE namespace=?", (namespace,)
                )
            self._conn.commit()
            return cur.rowcount

    def purge_expired(self, *, now: float | None = None) -> int:
        now = time.time() if now is None else now
        removed = 0
        with self._lock:
            for namespace, ttl in DEFAULT_TTL.items():
                if ttl is None:
                    continue
                cur = self._conn.execute(
                    "DELETE FROM entries WHERE namespace=? AND stored_at < ?",
                    (namespace, now - ttl),
                )
                removed += cur.rowcount
            self._conn.commit()
        self.stats.expirations += removed
        return removed

    def count(self, namespace: str | None = None) -> int:
        with self._lock:
            if namespace is None:
                row = self._conn.execute("SELECT COUNT(*) AS n FROM entries").fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM entries WHERE namespace=?", (namespace,)
                ).fetchone()
            return int(row["n"])

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "Cache":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- typed helpers ---------------------------------------------------
    # Keys are built in one place so a lookup and its write can never disagree.

    @staticmethod
    def caption_key(video_id: str, lang: str, kind: str = "auto") -> str:
        return f"{video_id}|{lang}|{kind}"

    def get_captions(self, video_id: str, lang: str, kind: str = "auto") -> Any | None:
        return self.get(CAPTIONS, self.caption_key(video_id, lang, kind))

    def put_captions(
        self, video_id: str, lang: str, value: Any, kind: str = "auto"
    ) -> None:
        self.put(CAPTIONS, self.caption_key(video_id, lang, kind), value)

    @staticmethod
    def search_key(provider: str, query: str, limit: int) -> str:
        return f"{provider}|{limit}|{query.strip().casefold()}"

    def get_search(self, provider: str, query: str, limit: int) -> Any | None:
        return self.get(SEARCH, self.search_key(provider, query, limit))

    def put_search(self, provider: str, query: str, limit: int, value: Any) -> None:
        self.put(SEARCH, self.search_key(provider, query, limit), value)

    @staticmethod
    def probe_key(provider: str, item_id: str) -> str:
        return f"{provider}|{item_id}"

    def get_probe(self, provider: str, item_id: str) -> Any | None:
        return self.get(PROBE, self.probe_key(provider, item_id))

    def put_probe(self, provider: str, item_id: str, value: Any) -> None:
        self.put(PROBE, self.probe_key(provider, item_id), value)
