"""The YouTube tab.

Search is yt-dlp's flat `ytsearch` — fast, no player, no extraction. Preview is
the official IFrame embed hosted in QtWebEngine, which downloads nothing.

Ceiling is 128k opus / 129k AAC. There is no 320 here; MP3 320 stays on the
menu because you asked for it, marked as the upscale it is.
"""

from __future__ import annotations

from urllib.parse import urlencode

from ..core.engine import YOUTUBE_SEARCH
from ._ytdlp import YtDlpProvider
from .base import Embed, Result

WATCH_URL = "https://www.youtube.com/watch?v={id}"
EMBED_URL = "https://www.youtube-nocookie.com/embed/{id}"


class YouTubeProvider(YtDlpProvider):
    key = "youtube"
    label = "YouTube"
    search_prefix = YOUTUBE_SEARCH
    artist_fields = ("uploader", "channel", "creator", "artist")

    def watch_url(self, result: Result) -> str:
        return result.url or WATCH_URL.format(id=result.id)

    def preview(self, result: Result, start: float | None = None) -> Embed:
        """Official IFrame embed, the same approach samplette-local already
        uses. Nothing is downloaded to audition a video or a phrase hit.

        youtube-nocookie.com rather than youtube.com: the preview pane has no
        reason to set advertising cookies in the app's web profile.
        """
        params: dict[str, str | int] = {
            "autoplay": 1,
            "playsinline": 1,
            "rel": 0,
            "modestbranding": 1,
        }
        if start is not None and start > 0:
            # The IFrame API takes whole seconds; the fractional part of a
            # phrase hit is applied by seekTo once the player is ready.
            params["start"] = int(start)
        url = EMBED_URL.format(id=result.id) + "?" + urlencode(params)
        return Embed(url=url, start=start)
