"""Upstream data sources — the same set Samplette credits on its metadata panel:
Discogs (catalog + genre/style/label/region), AcousticBrainz (key + tempo),
YouTube (the video itself), with MusicBrainz bridging Discogs -> AcousticBrainz.
"""
import threading
import time
from typing import Any, Dict, Optional

import requests

from ..config import USER_AGENT


class RateLimiter:
    """Thread-safe minimum-interval gate, shared by all callers of one API."""

    def __init__(self, per_minute: int):
        self.interval = 60.0 / max(1, per_minute)
        self._lock = threading.Lock()
        self._next_at = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            sleep_for = self._next_at - now
            if sleep_for > 0:
                time.sleep(sleep_for)
                now = time.monotonic()
            self._next_at = now + self.interval


_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})


def get_json(
    url: str,
    limiter: RateLimiter,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 25,
    retries: int = 2,
) -> Optional[Any]:
    """GET returning parsed JSON, or None on any failure.

    Callers are background workers that should degrade quietly rather than die,
    so every error path returns None instead of raising.
    """
    for attempt in range(retries + 1):
        limiter.wait()
        try:
            r = _session.get(url, params=params, headers=headers, timeout=timeout)
        except requests.RequestException:
            if attempt == retries:
                return None
            time.sleep(1.5 * (attempt + 1))
            continue

        if r.status_code == 200:
            try:
                return r.json()
            except ValueError:
                return None
        if r.status_code in (429, 502, 503, 504):
            # Back off and retry; Discogs uses 429 aggressively when unauthed.
            time.sleep(float(r.headers.get("Retry-After", 2 * (attempt + 1))))
            continue
        # 404 and friends are normal misses, not worth retrying.
        return None
    return None
