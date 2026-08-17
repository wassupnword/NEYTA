"""Shared implementation for the two providers that ride on yt-dlp.

Not in the build plan's file list — the plan has base.py, youtube.py and
soundcloud.py. YouTube and SoundCloud turned out to differ only in a search
prefix, an embed URL and how an artist name is spelled in the metadata, so the
alternative was the same eighty lines twice. base.py stays the pure contract;
this is the yt-dlp-specific half that soulseek.py will not inherit.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any

from .. import config
from ..core import convert
from ..core.engine import (
    Engine, EngineError, ExtractionFailed, format_selector,
)
from .base import (
    Media, NotSupported, Preview, ProgressFn, Provider, Result, Stream,
)

log = logging.getLogger(__name__)

#: Fraction of a fetch spent downloading; the rest is conversion. Rough, but a
#: bar that moves at roughly the right rate beats one that jumps.
_DOWNLOAD_SHARE = 0.7


def _stream_from_format(f: dict[str, Any]) -> Stream | None:
    """One yt-dlp format dict as a Stream, or None if it is not media."""
    acodec = f.get("acodec")
    vcodec = f.get("vcodec")
    has_audio = acodec not in (None, "none")
    has_video = vcodec not in (None, "none")
    if not has_audio and not has_video:
        return None  # storyboards and other non-media entries
    if f.get("format_note") == "storyboard":
        return None

    # abr is the audio bitrate; tbr is the total. For an audio-only format they
    # are the same, and for a muxed one abr is the honest number to show.
    bitrate = f.get("abr") or (f.get("tbr") if not has_video else None)

    return Stream(
        id=str(f.get("format_id") or ""),
        ext=str(f.get("ext") or ""),
        bitrate_kbps=float(bitrate) if bitrate else None,
        codec=acodec if has_audio else vcodec,
        sample_rate=f.get("asr"),
        filesize=f.get("filesize") or f.get("filesize_approx"),
        has_video=has_video,
        note=str(f.get("format_note") or ""),
    )


def _is_usable(path: Path) -> bool:
    """A downloaded file that ffmpeg can actually read audio out of.

    SoundCloud's HLS answers a ranged request with a valid, empty 257-byte MP4
    and exit code 0. Size alone is not enough to catch that, so this decodes
    the header.
    """
    try:
        if not path.exists() or path.stat().st_size < 1024:
            return False
        info = convert.probe(path)
    except (convert.ConversionError, OSError):
        return False
    return bool(info.duration and info.duration > 0)


def _best_audio_kbps(formats: list[dict[str, Any]]) -> float | None:
    rates = [
        s.bitrate_kbps
        for s in (_stream_from_format(f) for f in formats)
        if s and not s.has_video and s.bitrate_kbps
    ]
    return max(rates) if rates else None


class YtDlpProvider(Provider):
    search_prefix = ""
    #: Where the artist name lives in this service's metadata, best first.
    artist_fields: tuple[str, ...] = ("uploader", "channel", "creator")
    #: Where the track name lives, best first. Some extractors put
    #: "Artist - Track" in `title` and the bare track name in `track`; using
    #: `title` there yields "Artist - Artist - Track.flac" on disk.
    title_fields: tuple[str, ...] = ("title",)
    #: Whether this tab can cut a track down to a span at all.
    #: Only YouTube does: phrase search lives on that tab, and its videos are
    #: long enough that transferring only the matched seconds is the point.
    #: SoundCloud downloads whole tracks and nothing there asks for a cut.
    supports_spans = True

    def __init__(self, engine: Engine | None = None) -> None:
        self.engine = engine or Engine()

    # -- mapping ---------------------------------------------------------

    def _artist(self, entry: dict[str, Any]) -> str | None:
        for field in self.artist_fields:
            if value := entry.get(field):
                return str(value)
        return None

    def _title(self, entry: dict[str, Any]) -> str | None:
        for field in self.title_fields:
            if value := entry.get(field):
                return str(value)
        return None

    def _url(self, entry: dict[str, Any]) -> str | None:
        # SoundCloud's flat entries put an api.soundcloud.com URL in `url` and
        # the human one in `webpage_url`. YouTube's flat entries have only
        # `url`, and it is already the watch URL.
        return entry.get("webpage_url") or entry.get("url")

    def _thumbnail(self, entry: dict[str, Any]) -> str | None:
        if thumb := entry.get("thumbnail"):
            return str(thumb)
        thumbs = entry.get("thumbnails") or []
        return str(thumbs[-1].get("url")) if thumbs else None

    def to_result(self, entry: dict[str, Any]) -> Result:
        formats = entry.get("formats") or []
        return Result(
            provider=self.key,
            id=str(entry.get("id") or ""),
            title=self._title(entry) or "untitled",
            artist=self._artist(entry),
            duration=float(entry["duration"]) if entry.get("duration") else None,
            url=self._url(entry),
            thumbnail=self._thumbnail(entry),
            # Flat search usually carries no format ladder. When it does
            # (SoundCloud sometimes does), use it rather than showing a dash.
            source_kbps=_best_audio_kbps(formats) if formats else None,
        )

    # -- contract --------------------------------------------------------

    def search(self, query: str, limit: int = 20) -> list[Result]:
        entries = self.engine.search(self.search_prefix, query, limit)
        return [self.to_result(e) for e in entries]

    def probe(self, result: Result) -> Media:
        if not result.url:
            raise ExtractionFailed(f"{result.title!r} has no URL to probe")
        info = self.engine.extract(result.url, provider=self.key)

        streams = tuple(
            s for s in (_stream_from_format(f) for f in info.get("formats") or []) if s
        )
        if not streams:
            raise ExtractionFailed(f"no playable streams for {result.title!r}")

        # Ask Media what the best stream is rather than taking the largest
        # bitrate: a lossless stream advertises no abr, so a max() over the
        # numbers picks Bandcamp's 128k preview over the FLAC beside it and
        # then reports "128k" for a lossless release.
        provisional = Media(result=result, streams=streams)

        # Re-derive the result from the full extraction: flat search sometimes
        # has a shorter title or no artist at all.
        enriched = Result(
            provider=result.provider,
            id=result.id or str(info.get("id") or ""),
            title=self._title(info) or result.title,
            artist=self._artist(info) or result.artist,
            duration=float(info["duration"]) if info.get("duration") else result.duration,
            url=result.url,
            thumbnail=self._thumbnail(info) or result.thumbnail,
            source_kbps=provisional.source_kbps,
            extra=result.extra,
        )
        return Media(result=enriched, streams=streams)

    def fetch(
        self,
        result: Result,
        fmt: config.OutputFormat,
        dest: Path,
        *,
        progress: ProgressFn | None = None,
        span: tuple[float, float] | None = None,
    ) -> Path:
        if not result.url:
            raise ExtractionFailed(f"{result.title!r} has no URL to fetch")
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)

        def stage(lo: float, hi: float):
            if progress is None:
                return None

            def report(fraction: float, message: str) -> None:
                progress(lo + (hi - lo) * fraction, message)

            return report

        with tempfile.TemporaryDirectory(prefix="neyta-fetch-") as tmp:
            source, remaining_span = self._acquire(
                result.url, Path(tmp), fmt, span, stage(0.0, _DOWNLOAD_SHARE)
            )

            got = source.suffix.lstrip(".").lower()
            if fmt.kind == "source" and (not fmt.ext or got == fmt.ext.lower()):
                # Passthrough: the bytes the artist uploaded, moved into place.
                # Remuxing a FLAC into a FLAC would only risk losing tags for
                # no gain, so the file is kept exactly as delivered.
                final = dest if fmt.ext else dest.with_suffix(source.suffix)
                final.parent.mkdir(parents=True, exist_ok=True)
                source.replace(final)
                if progress:
                    progress(1.0, "done")
                return final

            convert.transcode(
                source,
                dest,
                fmt,
                start=remaining_span[0] if remaining_span else None,
                end=remaining_span[1] if remaining_span else None,
                progress=stage(_DOWNLOAD_SHARE, 1.0),
            )
        return dest

    # -- acquisition -----------------------------------------------------

    def _acquire(
        self,
        url: str,
        tmp: Path,
        fmt: config.OutputFormat,
        span: tuple[float, float] | None,
        progress: ProgressFn | None,
    ) -> tuple[Path, tuple[float, float] | None]:
        """Get the source bytes, and say what cutting is still owed.

        Returns (file, span_still_to_cut). A section download applies the span
        during transfer, so None comes back; a full download leaves the cut to
        convert.py.
        """
        selector = format_selector(fmt)

        if span is not None:
            if not self.supports_spans:
                raise NotSupported(
                    f"the {self.label} tab downloads whole tracks; "
                    "cutting to a span is a YouTube feature"
                )
            try:
                source = self.engine.download(
                    url, tmp, selector=selector, span=span, progress=progress
                )
            except EngineError as exc:
                # Measured on real phrase hits: a ranged request against a
                # rights-restricted stream dies with "ffmpeg exited with code
                # 8" on roughly half of them, while a plain download of the
                # same video succeeds. Falling back costs bandwidth; failing
                # costs the user their clip.
                log.warning("ranged download for %s failed (%s); "
                            "refetching in full and cutting locally", url, exc)
                source = None
            else:
                if _is_usable(source):
                    return source, None
                # A ranged request that answers with a well-formed but empty
                # container and exit code 0 is worse than one that errors:
                # nothing downstream notices until the user plays silence.
                log.warning(
                    "ranged download for %s came back unusable; "
                    "refetching in full and cutting locally", url,
                )
            if source is not None:
                source.unlink(missing_ok=True)

        source = self.engine.download(
            url, tmp, selector=selector, progress=progress, filename_stem="full"
        )
        if not _is_usable(source):
            raise ExtractionFailed(
                f"downloaded file from {url} has no readable audio stream"
            )
        return source, span

    def preview(self, result: Result) -> Preview:
        raise NotImplementedError
