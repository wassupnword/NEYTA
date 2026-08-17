"""The contract all three sources implement.

Build plan 3.1: the search bar, result list, format picker, stem picker, job
queue and drag tray are written once and never learn which service they are
pointed at. The tab bar swaps the provider; nothing above this line changes.

Phase 2 implements YouTube and SoundCloud against this; phase 6 adds Soulseek.
The conformance suite in tests/test_provider_contract.py is what every
provider must satisfy.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from .. import config

ProgressFn = Callable[[float, str], None]


@dataclass(frozen=True)
class Result:
    """One row in the result list, on any tab."""

    provider: str
    id: str
    title: str
    artist: str | None = None
    duration: float | None = None
    url: str | None = None
    thumbnail: str | None = None
    #: The true bitrate of the best available audio, in kbps. Shown on the row
    #: so the format picker's upscale marking has something honest to compare
    #: against (build plan 2.1). None when it cannot be known before a probe.
    source_kbps: float | None = None
    #: Free-slot / queue state on Soulseek; None elsewhere.
    availability: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def display_bitrate(self) -> str:
        if self.source_kbps is None:
            return "—"
        return f"{self.source_kbps:.0f}k"


#: Codecs and containers that carry the whole signal. A lossless stream has
#: no meaningful "bitrate to beat" — comparing its 774k FLAC against a 320k
#: MP3 as though they were the same axis is what produces a wrong upscale
#: warning, so `Stream.lossless` is ranked ahead of any number.
LOSSLESS_CODECS = ("flac", "alac", "pcm", "wav", "aiff", "ape", "wavpack", "tta")
LOSSLESS_EXTS = ("flac", "wav", "aiff", "aif", "alac", "ape", "wv")


@dataclass(frozen=True)
class Stream:
    """One downloadable audio or video stream behind a Result."""

    id: str
    ext: str
    #: kbps as measured, not as advertised. None for lossless streams, where
    #: it is not a fixed property of the format.
    bitrate_kbps: float | None
    codec: str | None = None
    sample_rate: int | None = None
    filesize: int | None = None
    has_video: bool = False
    note: str = ""

    @property
    def lossless(self) -> bool:
        codec = (self.codec or "").lower()
        if any(c in codec for c in LOSSLESS_CODECS):
            return True
        return self.ext.lower() in LOSSLESS_EXTS


@dataclass(frozen=True)
class Media:
    """The outcome of `probe`: what this result can actually give you."""

    result: Result
    streams: tuple[Stream, ...]
    #: Present on Soulseek, where the peer's file is the source of truth.
    lossless: bool = False

    @property
    def best_audio(self) -> Stream | None:
        """Lossless first, then bitrate.

        Ranking on bitrate alone picks Bandcamp's 128k MP3 over the FLAC
        sitting beside it, because a FLAC stream carries no advertised abr.
        """
        audio = [s for s in self.streams if not s.has_video]
        if not audio:
            return None
        return max(audio, key=lambda s: (s.lossless, s.bitrate_kbps or 0.0))

    @property
    def has_lossless(self) -> bool:
        return any(s.lossless for s in self.streams if not s.has_video)

    @property
    def source_kbps(self) -> float | None:
        """The number to compare an output format against.

        None when the best stream is lossless — there is nothing to upscale
        past, so no lossy option gets marked. That is the honest answer: MP3
        320 from a FLAC really is a downgrade, not an inflation.
        """
        best = self.best_audio
        if best is None or best.lossless:
            return None
        return best.bitrate_kbps

    @property
    def quality_label(self) -> str:
        """What to show in the result row instead of a bitrate."""
        best = self.best_audio
        if best is None:
            return "—"
        if best.lossless:
            return (best.ext or best.codec or "lossless").upper()
        return f"{best.bitrate_kbps:.0f}k" if best.bitrate_kbps else "—"


@dataclass(frozen=True)
class Embed:
    """A player URL to host in QtWebEngine. Nothing is downloaded."""

    url: str
    #: Seconds to seek to on load — used by phrase hits.
    start: float | None = None
    #: False for players such as Bandcamp that require their own play button.
    autoplay: bool = True


@dataclass(frozen=True)
class LocalFile:
    """Soulseek's answer to preview: there is no stream to scrub, so the file
    is fetched first and played locally (build plan 2.4)."""

    path: Path
    #: True when this was fetched purely to audition and may be discarded.
    temporary: bool = True


@dataclass(frozen=True)
class FormatOption:
    """One row of the format picker, as it applies to one probed item."""

    format: config.OutputFormat
    #: Why this option is marked, or None when it is unremarkable.
    note: str | None = None
    #: False when the service cannot deliver this container for this item.
    available: bool = True

    @property
    def is_upscale(self) -> bool:
        return self.available and self.note is not None


Preview = Embed | LocalFile
Availability = Literal["free", "queued", "unknown"]


class ProviderError(Exception):
    pass


class NotSupported(ProviderError):
    """Raised by a provider asked for something its service cannot do."""


class Provider(abc.ABC):
    """Four calls. Everything above the provider layer uses only these."""

    #: Matches the keys in config.SOURCE_CEILING_KBPS and config.FORMATS.
    key: str = ""
    label: str = ""

    @property
    def ceiling_kbps(self) -> float | None:
        return config.SOURCE_CEILING_KBPS.get(self.key)

    @property
    def ceiling_note(self) -> str:
        return config.CEILING_NOTE.get(self.key, "")

    @property
    def preview_label(self) -> str:
        return "Preview"

    @property
    def preview_requires_transfer(self) -> bool:
        return False

    @abc.abstractmethod
    def search(self, query: str, limit: int = 20) -> list[Result]:
        """Results for a query. May be slow; always called from a job."""

    @abc.abstractmethod
    def probe(self, result: Result) -> Media:
        """The streams behind a result, with true bitrates."""

    @abc.abstractmethod
    def fetch(
        self,
        result: Result,
        fmt: config.OutputFormat,
        dest: Path,
        *,
        progress: ProgressFn | None = None,
    ) -> Path:
        """Download and convert to `dest`. Returns the path actually written,
        which may differ from `dest` if a collision was resolved."""

    @abc.abstractmethod
    def preview(self, result: Result) -> Preview:
        """An embed URL, or a local file for sources that cannot stream."""

    # -- shared helpers --------------------------------------------------

    def formats(self) -> tuple[config.OutputFormat, ...]:
        return config.formats_for(self.key)

    def annotated_formats(
        self, source_kbps: float | None
    ) -> list[tuple[config.OutputFormat, str | None]]:
        return config.annotate_formats(self.key, source_kbps)

    def format_options(self, media: "Media | None" = None) -> list["FormatOption"]:
        """Every format for this tab, with what is true of it for this item.

        Needs the probe result, because two things are per-item rather than
        per-service: whether an upscale would be an upscale, and whether a
        specific container exists at all. Bandcamp offers FLAC on a release
        the artist made downloadable and nothing but a 128k preview on the one
        beside it.
        """
        source_kbps = media.source_kbps if media else None
        available_exts = (
            {s.ext.lower() for s in media.streams if not s.has_video}
            if media else set()
        )
        options: list[FormatOption] = []
        for fmt in self.formats():
            note = config.upscale_note(fmt, source_kbps)
            available = True
            if media is not None and fmt.kind == "source" and fmt.ext:
                available = fmt.ext.lower() in available_exts
                if not available:
                    note = "not offered for this release"
            options.append(FormatOption(fmt, note=note, available=available))
        return options
