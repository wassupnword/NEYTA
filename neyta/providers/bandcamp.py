"""The Bandcamp tab.

The only tab besides Soulseek that can hand you a genuine lossless file, and
the only one where doing so is what the artist intended. Measured on a
free-download track, unauthenticated:

    flac     17.4 MB   774k   44100Hz
    wav      31.8 MB  1411k   44100Hz
    mp3-320   7.3 MB   320k          <- a real 320, not an upscale
    alac     18.0 MB   798k   44100Hz

Where the artist has not enabled downloading, the same extractor returns only
`mp3-128` — the streaming preview. So this tab has no fixed ceiling: it is a
property of the release, not of the service, and the result row reports what
the release actually offers.

yt-dlp has Bandcamp extractors but no Bandcamp search, so search goes through
the public autocomplete endpoint the site's own search box uses. Everything
after that — probe, fetch, convert — is the shared yt-dlp path.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from ..core.engine import ExtractionFailed
from ._ytdlp import YtDlpProvider
from .base import Embed, Media, Result

log = logging.getLogger(__name__)

SEARCH_URL = "https://bandcamp.com/api/bcsearch_public_api/1/autocomplete_elastic"
EMBED_URL = "https://bandcamp.com/EmbeddedPlayer/track={id}/size=large/artwork=small/"

#: The endpoint 400s or returns a differently-shaped body when these are
#: absent, so they are sent even though both are empty.
_SEARCH_DEFAULTS = {"full_page": False, "fan_id": None}

#: 't' tracks, 'a' albums, 'b' artists. Tracks are what a sampler wants.
TRACK_FILTER = "t"

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"


class BandcampProvider(YtDlpProvider):
    key = "bandcamp"
    label = "Bandcamp"
    artist_fields = ("artist", "uploader", "creator")
    # yt-dlp's Bandcamp `title` is "Artist - Track"; `track` is the bare name.
    # Without this a file lands as "oylumtanis - oylumtanis - Archangel.flac".
    title_fields = ("track", "title")
    #: Tracks are single songs on a store page; nothing here asks for a cut.
    supports_spans = False

    def __init__(self, engine=None, session: Any | None = None) -> None:
        super().__init__(engine)
        self._session = session or requests.Session()
        self._session.headers.update({"User-Agent": _UA})

    # -- search ----------------------------------------------------------

    def search(self, query: str, limit: int = 20) -> list[Result]:
        query = query.strip()
        if not query:
            return []

        cache = self.engine.cache
        if cache is not None:
            hit = cache.get_search(self.key, query, limit)
            if hit is not None:
                return [self._result_from_hit(h) for h in hit]

        try:
            response = self._session.post(
                SEARCH_URL,
                json={"search_text": query, "search_filter": TRACK_FILTER,
                      **_SEARCH_DEFAULTS},
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise ExtractionFailed(f"Bandcamp search failed: {exc}") from exc
        except ValueError as exc:
            raise ExtractionFailed("Bandcamp search returned no JSON") from exc

        hits = (payload.get("auto") or {}).get("results") or []
        hits = [h for h in hits if h.get("type") == TRACK_FILTER][:limit]

        if cache is not None:
            cache.put_search(self.key, query, limit, hits)
        return [self._result_from_hit(h) for h in hits]

    def _result_from_hit(self, hit: dict[str, Any]) -> Result:
        # item_url_path is already absolute. Joining it onto item_url_root
        # produces "https://x.bandcamp.comhttps://x.bandcamp.com/track/y",
        # which resolves to nothing.
        url = hit.get("item_url_path") or hit.get("item_url_root")
        return Result(
            provider=self.key,
            id=str(hit.get("id") or ""),
            title=str(hit.get("name") or "untitled"),
            artist=str(hit.get("band_name")) if hit.get("band_name") else None,
            # Search carries no duration and no format ladder; both arrive on
            # probe, which is also when we learn whether this release is
            # downloadable at all.
            duration=None,
            url=url,
            thumbnail=hit.get("img") or None,
            source_kbps=None,
            extra={"album": hit.get("album_name"), "band_id": hit.get("band_id")},
        )

    # -- probe -----------------------------------------------------------

    def probe(self, result: Result) -> Media:
        media = super().probe(result)
        # `lossless` here means the artist enabled downloading, which is the
        # single most useful thing to know about a Bandcamp result.
        return Media(
            result=media.result,
            streams=media.streams,
            lossless=media.has_lossless,
        )

    # -- preview ---------------------------------------------------------

    def preview(self, result: Result, start: float | None = None) -> Embed:
        """Bandcamp's official embedded player. Nothing is downloaded, and the
        artist gets the play count. Bandcamp intentionally does not support
        autoplay, so its own play button remains the required final gesture."""
        return Embed(
            url=EMBED_URL.format(id=result.id), start=start, autoplay=False
        )
