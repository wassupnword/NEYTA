"""The SoundCloud tab.

Search is yt-dlp's native `scsearch`, verified working unauthenticated.

On not using node-soundcloud-downloader: yt-dlp already ships nine SoundCloud
extractors including search, sets, users and playlists, in the same Python
process that already does YouTube. A Node package would add a second runtime,
a second dependency tree, a subprocess bridge and a separate auth path to
reach the exact same HLS and progressive streams. Node 24 is installed here if
something later genuinely needs it; nothing here does.

Ceiling is 160k AAC (hls_aac_160k), with 128k MP3 alongside it. Where an
artist has enabled the original-file download, that genuinely higher-quality
original is detected and offered as its own option.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from ..core.engine import SOUNDCLOUD_SEARCH
from ._ytdlp import YtDlpProvider
from .base import Embed, Media, Result

WIDGET_URL = "https://w.soundcloud.com/player/"

#: yt-dlp names the artist-enabled original download this way. It is the only
#: SoundCloud stream that can beat 160k AAC.
_ORIGINAL_MARKERS = ("download", "original")


class SoundCloudProvider(YtDlpProvider):
    key = "soundcloud"
    label = "SoundCloud"
    search_prefix = SOUNDCLOUD_SEARCH
    # SoundCloud fills `uploader` with the account name and, on properly
    # tagged tracks, `artists`/`artist` with the actual credit.
    artist_fields = ("artist", "uploader", "creator")
    #: This tab downloads whole tracks. Nothing here asks for a cut — phrase
    #: search is a YouTube feature. (SoundCloud's HLS would not honour a
    #: ranged request anyway: it answers with a well-formed but empty
    #: 257-byte MP4 and exit code 0.)
    supports_spans = False

    def _artist(self, entry: dict[str, Any]) -> str | None:
        if artists := entry.get("artists"):
            if isinstance(artists, (list, tuple)) and artists:
                return str(artists[0])
        return super()._artist(entry)

    def probe(self, result: Result) -> Media:
        media = super().probe(result)
        # An original-file stream is lossless or near it, and it is the reason
        # this tab is not always capped at 160k.
        has_original = any(
            any(m in s.id.lower() for m in _ORIGINAL_MARKERS) for s in media.streams
        )
        if not has_original:
            return media
        return Media(result=media.result, streams=media.streams, lossless=True)

    def preview(self, result: Result, start: float | None = None) -> Embed:
        """Official HTML5 widget embed. No download."""
        params: dict[str, Any] = {
            "url": result.url or "",
            "auto_play": "true",
            "show_comments": "false",
            "visual": "false",
        }
        url = WIDGET_URL + "?" + urlencode(params)
        return Embed(url=url, start=start)
