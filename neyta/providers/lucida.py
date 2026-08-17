"""The Spotify tab.

Streaming services are what most people mean by "where the music is", and none
of the other four tabs is one. This tab is a client of lucida-flow, which
drives lucida.to; lucida.to is the thing that actually reaches the service. The
tab is called Spotify because that is the name of the shelf people look on —
lucida-flow will equally accept Tidal, Qobuz, Deezer and Amazon Music, and the
service is a parameter here rather than five more tabs.

What it does differently from the other four, all of it forced by what is on
the other end:

  * search returns a name, an artist and a URL, and nothing else. No duration,
    no bitrate. The result rows say "—" rather than a number they do not have,
    and the quality is only known after a probe — sometimes only after the
    download.
  * there is no stream to scrub, so preview is fetch-then-play, exactly like
    Soulseek. The button says so.
  * the server behind it is a headless browser. A search is seconds, and a
    download can be much longer than a file of that size suggests.

Worth being plain about, since the app should not pretend otherwise: this tab
takes paid streaming catalogue through a third-party ripper. That is a
different footing from YouTube and Bandcamp, and it is why nothing here starts
by itself — the server is only launched once you use the tab.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import requests

from .. import config
from ..core import convert
from .base import (
    Media, NotSupported, Preview, ProgressFn, Provider, ProviderError, Result,
    Stream,
)

log = logging.getLogger(__name__)

#: Words lucida-flow puts in its `quality` string when a track is lossless.
LOSSLESS_WORDS = ("flac", "lossless", "hi-res", "hires", "24-bit", "alac")

#: Extensions we may be handed. Whatever arrives is what the service had.
DEFAULT_EXT = "flac"


class LucidaUnavailable(ProviderError):
    """No checkout, no server, or the server would not answer."""


def _looks_lossless(quality: str | None) -> bool:
    text = (quality or "").lower()
    return any(word in text for word in LOSSLESS_WORDS)


class LucidaProvider(Provider):
    key = "spotify"
    label = "Spotify"
    #: Whole tracks. There is no way to ask lucida.to for part of one.
    supports_spans = False

    def __init__(
        self,
        bootstrap: Any | None = None,
        session: Any | None = None,
        service: str = config.LUCIDA_SERVICE,
        timeout: float = 120.0,
    ) -> None:
        self.bootstrap = bootstrap
        self.session = session or requests.Session()
        self.service = service
        self.timeout = timeout

    # -- the local server -------------------------------------------------

    @property
    def url(self) -> str:
        if self.bootstrap is None:
            raise LucidaUnavailable("no lucida-flow to talk to")
        return self.bootstrap.url

    def ensure_server(self) -> None:
        """Start lucida-flow if it is not already up.

        Called at the top of every request rather than once at launch: the tab
        may never be opened, and starting a headless browser for a tab nobody
        pressed is exactly the kind of thing that makes an app feel heavy.
        """
        if self.bootstrap is None:
            raise LucidaUnavailable(
                "lucida-flow is not configured — see the Spotify section in "
                "Settings"
            )
        try:
            self.bootstrap.start()
        except Exception as exc:  # noqa: BLE001 — one message, not a traceback
            raise LucidaUnavailable(str(exc)) from exc

    def _post(self, path: str, payload: dict[str, Any], **kwargs: Any) -> Any:
        self.ensure_server()
        try:
            response = self.session.post(
                f"{self.url}{path}", json=payload,
                timeout=kwargs.pop("timeout", self.timeout), **kwargs,
            )
        except requests.RequestException as exc:
            raise LucidaUnavailable(f"lucida-flow did not answer: {exc}") from exc
        if response.status_code != 200:
            raise LucidaUnavailable(
                f"lucida-flow answered {response.status_code} for {path}"
            )
        return response

    def _json(self, path: str, payload: dict[str, Any], **kwargs: Any) -> dict:
        response = self._post(path, payload, **kwargs)
        try:
            data = response.json()
        except ValueError as exc:
            raise LucidaUnavailable("lucida-flow returned a non-JSON body") from exc
        if isinstance(data, dict) and data.get("error"):
            raise LucidaUnavailable(str(data["error"]))
        return data if isinstance(data, dict) else {}

    # -- mapping ----------------------------------------------------------

    def to_result(self, track: dict[str, Any]) -> Result:
        url = str(track.get("url") or "")
        return Result(
            provider=self.key,
            # lucida.to has no id of its own; the service's URL is what
            # identifies a track, and it is what every later call passes back.
            id=url,
            title=str(track.get("name") or "untitled"),
            artist=(str(track["artist"]) if track.get("artist") else None),
            # Not available before a probe, and often not before a download.
            duration=None,
            url=url or None,
            source_kbps=None,
            extra={
                "album": track.get("album"),
                "service": track.get("service") or self.service,
            },
        )

    # -- contract ---------------------------------------------------------

    def search(self, query: str, limit: int = 20) -> list[Result]:
        query = query.strip()
        if not query:
            return []
        data = self._json("/search", {
            "query": query, "service": self.service, "limit": limit,
        })
        tracks = data.get("tracks") or []
        results = [
            self.to_result(track) for track in tracks
            if isinstance(track, dict) and track.get("url")
        ]
        return results[:limit]

    def probe(self, result: Result) -> Media:
        """Ask what the service says about this track.

        lucida-flow reports quality as a phrase rather than a number, so the
        stream is built from what that phrase implies: lossless or not, and no
        invented bitrate for either. A lossless stream has no meaningful
        "bitrate to beat", which the format picker already understands.
        """
        if not result.url:
            raise LucidaUnavailable("this result carries no URL to probe")
        info = self._json("/info", {"url": result.url})
        quality = info.get("quality")
        lossless = _looks_lossless(quality)
        ext = DEFAULT_EXT if lossless else "m4a"
        stream = Stream(
            id=result.id,
            ext=ext,
            bitrate_kbps=None,
            codec=ext,
            note=str(quality or ""),
        )
        return Media(result=result, streams=(stream,), lossless=lossless)

    def fetch(
        self,
        result: Result,
        fmt: config.OutputFormat,
        dest: Path,
        *,
        progress: ProgressFn | None = None,
        span: tuple[float, float] | None = None,
    ) -> Path:
        """Download through the local server, then convert if asked.

        The server writes the file itself and reports where it landed, so
        nothing is streamed through this process. The download is one long
        opaque step — a headless browser is doing it — so progress moves once
        at the start and once when the file exists, rather than pretending to
        a percentage.
        """
        if span is not None:
            raise NotSupported(
                "this tab downloads whole tracks; lucida.to has no way to "
                "ask for part of one"
            )
        if not result.url:
            raise LucidaUnavailable("this result carries no URL to download")

        if progress:
            progress(0.05, "asking lucida for the file")
        data = self._json("/download", {"url": result.url})
        if not data.get("success"):
            raise LucidaUnavailable(
                str(data.get("error") or "the download did not complete")
            )
        source = Path(str(data.get("filepath") or ""))
        if not source.exists():
            raise LucidaUnavailable(
                f"lucida-flow reported a file at {source}, and it is not there"
            )
        if progress:
            progress(0.8, "downloaded")
        return self._deliver(source, Path(dest), fmt, progress)

    def _deliver(
        self,
        source: Path,
        dest: Path,
        fmt: config.OutputFormat,
        progress: ProgressFn | None,
    ) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        got = source.suffix.lstrip(".").lower()
        if fmt.kind == "source" and (not fmt.ext or got == fmt.ext.lower()):
            # Passthrough: the service's own file. Re-encoding a FLAC you just
            # fetched into a WAV nobody asked for is how quality gets lost by
            # default.
            final = dest if fmt.ext else dest.with_suffix(source.suffix)
            source.replace(final)
            if progress:
                progress(1.0, "done")
            return final
        convert.transcode(
            source, dest, fmt,
            progress=(lambda f, m="": progress(0.8 + 0.2 * f, m))
            if progress else None,
        )
        return dest

    def preview(self, result: Result) -> Preview:
        """There is nothing to audition short of fetching the whole track.

        Soulseek answers this with fetch-then-play, and the same trick would
        work here — but a Soulseek transfer is a file copy, while this one
        drives a headless browser through a rip that can take minutes. Doing
        that from the preview button, on the way to deciding whether you even
        want the track, is not a fair trade. The download is the way to hear
        it, and saying so is more honest than a wait with no explanation.
        """
        raise NotSupported(
            "No preview on this tab — there is no stream to scrub, and "
            "fetching the whole track takes a browser session. Download it "
            "to hear it."
        )
