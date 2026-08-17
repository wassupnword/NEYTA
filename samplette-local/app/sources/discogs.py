"""Discogs: the catalog. Supplies artist, release, year, label, region,
genre, style and the two copyright lines Samplette shows.

Works unauthenticated; a DISCOGS_TOKEN just raises the rate limit.
"""
import re
from typing import Any, Dict, List, Optional

from ..config import DISCOGS_RPM, DISCOGS_TOKEN
from . import RateLimiter, get_json

API = "https://api.discogs.com"
_limiter = RateLimiter(DISCOGS_RPM)

# Discogs disambiguates duplicate artist names with a trailing "(2)", "(17)".
_ARTIST_SUFFIX = re.compile(r"\s*\(\d+\)\s*$")
# Tracklist positions for headings/sides we don't want as tracks.
_SKIP_TYPES = {"heading", "index"}


def _auth_params() -> Dict[str, str]:
    return {"token": DISCOGS_TOKEN} if DISCOGS_TOKEN else {}


def search_releases(
    genre: Optional[str] = None,
    style: Optional[str] = None,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    country: Optional[str] = None,
    query: Optional[str] = None,
    page: int = 1,
    per_page: int = 50,
) -> Dict[str, Any]:
    """One page of release IDs matching a facet. Returns {ids, pages}."""
    params: Dict[str, Any] = {
        "type": "release",
        "page": page,
        "per_page": per_page,
        **_auth_params(),
    }
    if query:
        params["q"] = query
    if genre:
        params["genre"] = genre
    if style:
        params["style"] = style
    if country:
        params["country"] = country
    if year_from and year_to:
        params["year"] = "[{} TO {}]".format(year_from, year_to)
    elif year_from:
        params["year"] = year_from

    data = get_json(API + "/database/search", _limiter, params=params)
    if not data:
        return {"ids": [], "pages": 0}
    ids = [r["id"] for r in data.get("results", []) if r.get("id")]
    pages = int((data.get("pagination") or {}).get("pages") or 0)
    return {"ids": ids, "pages": pages}


def clean_artist(name: str) -> str:
    return _ARTIST_SUFFIX.sub("", name or "").strip()


def _join_artists(credits: List[Dict[str, Any]]) -> str:
    """Rebuild a display artist from Discogs' artist array, honoring joins."""
    out = []
    for a in credits or []:
        name = clean_artist(a.get("anv") or a.get("name") or "")
        if not name:
            continue
        out.append(name)
        join = (a.get("join") or "").strip()
        if join and join != ",":
            out.append(join)
        elif join == ",":
            out.append(",")
    text = " ".join(out).replace(" ,", ",").strip()
    return re.sub(r"[,&/]+$", "", text).strip()


def _copyright_lines(release: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """Pull the (C) and (P) lines out of the companies list."""
    out: Dict[str, Optional[str]] = {"copyright": None, "p_copyright": None}
    for c in release.get("companies") or []:
        entity = (c.get("entity_type_name") or "").lower()
        name = clean_artist(c.get("name") or "")
        if not name:
            continue
        if "phonographic" in entity and not out["p_copyright"]:
            out["p_copyright"] = name
        elif entity.startswith("copyright") and not out["copyright"]:
            out["copyright"] = name
    return out


def get_release_tracks(release_id: int) -> List[Dict[str, Any]]:
    """Expand one release into track dicts ready for the library."""
    data = get_json(
        "{}/releases/{}".format(API, release_id), _limiter, params=_auth_params()
    )
    if not data:
        return []

    release_artist = _join_artists(data.get("artists") or [])
    labels = [clean_artist(l.get("name", "")) for l in data.get("labels") or []]
    label = next((l for l in labels if l), None)
    year = data.get("year") or None
    if year:
        try:
            year = int(year) or None
        except (TypeError, ValueError):
            year = None

    genres = [g for g in data.get("genres") or [] if g]
    styles = [s for s in data.get("styles") or [] if s]
    region = data.get("country") or None
    copy_lines = _copyright_lines(data)
    release_title = data.get("title") or None

    tracks = []
    for t in data.get("tracklist") or []:
        if (t.get("type_") or "track") in _SKIP_TYPES:
            continue
        title = (t.get("title") or "").strip()
        if not title:
            continue
        # A track can credit its own artist on compilations.
        artist = _join_artists(t.get("artists") or []) or release_artist
        if not artist or artist.lower() == "various":
            continue
        tracks.append({
            "artist": artist,
            "title": title,
            "release": release_title,
            "year": year,
            "label": label,
            "region": region,
            "copyright": copy_lines["copyright"],
            "p_copyright": copy_lines["p_copyright"],
            "genres": genres,
            "styles": styles,
            "tags": sorted(set(genres + styles)),
            "discogs_release_id": release_id,
        })
    return tracks
