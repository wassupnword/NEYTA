"""Finding the video for a catalog track.

We use yt-dlp only to *search* (flat extraction — metadata for the result list,
no player/format extraction, no downloading). Playback happens in the official
YouTube IFrame embed in the browser, exactly like Samplette does it.
"""
import re
import threading
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional

try:
    from yt_dlp import YoutubeDL
except ImportError:  # pragma: no cover - surfaced at startup instead
    YoutubeDL = None

# Flat extraction returns the search page's own metadata and never touches the
# player, which is both much faster and immune to the bot checks that break
# full extraction.
_YDL_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "extract_flat": True,
    "ignoreerrors": True,
    "noprogress": True,
    "socket_timeout": 20,
}

_search_lock = threading.Semaphore(3)

_NOISE = re.compile(
    r"\b(official\s*(music\s*)?video|official\s*audio|lyric\s*video|hd|hq|"
    r"remaster(ed)?(\s*\d{4})?|full\s*album|audio|visualizer|4k|1080p)\b",
    re.I,
)
_BRACKETS = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_NONWORD = re.compile(r"[^a-z0-9]+")

# Titles that indicate we matched a whole record rather than the track.
_ALBUM_HINT = re.compile(r"\b(full\s*album|complete\s*album|mix|mixtape|"
                         r"compilation|megamix|dj\s*set|live\s*set)\b", re.I)


def _norm(text: str) -> str:
    text = _BRACKETS.sub(" ", (text or "").lower())
    text = _NOISE.sub(" ", text)
    return _NONWORD.sub(" ", text).strip()


def _ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def search(query: str, limit: int = 6) -> List[Dict[str, Any]]:
    """Flat YouTube search. Returns raw-ish entry dicts."""
    if YoutubeDL is None:
        return []
    with _search_lock:
        try:
            with YoutubeDL(_YDL_OPTS) as ydl:
                info = ydl.extract_info(
                    "ytsearch{}:{}".format(limit, query), download=False
                )
        except Exception:
            return []

    out = []
    for e in (info or {}).get("entries") or []:
        if not e or not e.get("id"):
            continue
        channel = e.get("channel") or e.get("uploader") or ""
        out.append({
            "yt_video_id": e["id"],
            "yt_title": e.get("title") or "",
            "yt_channel": channel,
            "yt_channel_id": e.get("channel_id") or e.get("uploader_id") or "",
            "yt_is_topic": 1 if channel.strip().endswith("- Topic") else 0,
            "yt_views": e.get("view_count"),
            "yt_duration": int(e["duration"]) if e.get("duration") else None,
            "live": bool(e.get("is_live") or e.get("live_status") == "is_live"),
        })
    return out


def _score(cand: Dict[str, Any], artist: str, title: str) -> float:
    """Rank a search hit against the track we actually wanted."""
    yt_title = cand.get("yt_title") or ""
    norm_yt = _norm(yt_title)
    norm_want = _norm("{} {}".format(artist, title))
    norm_title_only = _norm(title)

    score = _ratio(norm_yt, norm_want) * 2.0

    # The track name appearing verbatim matters more than overall similarity.
    if norm_title_only and norm_title_only in norm_yt:
        score += 1.2
    if _norm(artist) and _norm(artist) in norm_yt:
        score += 0.6
    # "Artist - Topic" channels are auto-generated from the actual release,
    # so they're the most reliable match available.
    if cand.get("yt_is_topic"):
        score += 1.0
    if _norm(artist) and _ratio(_norm(cand.get("yt_channel") or ""),
                                _norm(artist)) > 0.8:
        score += 0.4

    duration = cand.get("yt_duration") or 0
    if _ALBUM_HINT.search(yt_title) or duration > 1200:
        score -= 1.5          # probably a full album rip, not this track
    if duration and duration < 45:
        score -= 1.0          # clip or teaser
    if cand.get("live"):
        score -= 0.8
    return score


def find_for_track(artist: str, title: str) -> Optional[Dict[str, Any]]:
    """Best video for a track, or None if nothing plausible turned up."""
    results = search("{} {}".format(artist, title), limit=6)
    if not results:
        return None

    best = max(results, key=lambda c: _score(c, artist, title))
    if _score(best, artist, title) < 1.0:
        return None
    best.pop("live", None)
    return best
