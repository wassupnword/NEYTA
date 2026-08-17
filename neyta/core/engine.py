"""yt-dlp facade: search, extract, download.

One place that knows yt-dlp's option dictionary and its exception text, so the
providers above stay small and the whole surface can be faked in tests.

Two things this deliberately does NOT do:

  * override `player_client`. The old 2025.10.14 release in this project only
    worked with `player_client=android`, and that override caps YouTube at
    format 18 — muxed 360p, ~128k AAC, no audio-only streams at all. On a
    current release the default client selection returns the full DASH ladder,
    so the override is unnecessary and actively halves audio quality.
  * send cookies unless the user has explicitly provided a cookie file.
    Everything works unauthenticated; `cookiesfrombrowser` is never set, so
    NEYTA never reaches into a browser's cookie store.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from .. import config
from .cache import Cache

log = logging.getLogger(__name__)

ProgressFn = Callable[[float, str], None]

YOUTUBE_SEARCH = "ytsearch"
SOUNDCLOUD_SEARCH = "scsearch"


class EngineError(RuntimeError):
    pass


class RateLimited(EngineError):
    """HTTP 429. Hit for real during caption fetching, so backoff and caching
    are load-bearing rather than polish (build plan section 0)."""


class Unavailable(EngineError):
    """Private, deleted, region-blocked or age-gated."""


class ExtractionFailed(EngineError):
    pass


_RATE_LIMIT_MARKERS = ("429", "too many requests", "rate limit", "rate-limit")
_UNAVAILABLE_MARKERS = (
    "video unavailable", "private video", "this video is unavailable",
    "removed by the uploader", "not available in your country",
    "sign in to confirm your age", "members-only", "requested format is not available",
    # SoundCloud serves some label-uploaded tracks under DRM. Nothing can
    # fetch those, and retrying will not change it, so it belongs here rather
    # than in the generic failure bucket.
    "drm protected", "is drm",
    # A 403 on the media URL itself — as opposed to on metadata — is a
    # rights-restricted stream. Measured at roughly 45% of samplette's
    # library: the metadata and the format ladder come back fine, then every
    # client 403s on the actual bytes. Retrying does not help, and no player
    # client works, so it is permanent rather than transient.
    "unable to download video data: http error 403",
)


def classify(exc: BaseException) -> EngineError:
    """Map yt-dlp's stringly-typed errors onto something callers can branch on."""
    text = str(exc).lower()
    if any(m in text for m in _RATE_LIMIT_MARKERS):
        return RateLimited(str(exc))
    if any(m in text for m in _UNAVAILABLE_MARKERS):
        return Unavailable(str(exc))
    return ExtractionFailed(str(exc))


def backoff_delays(
    retries: int = config.CAPTION_MAX_RETRIES,
    base: float = config.CAPTION_BACKOFF_BASE,
    cap: float = config.CAPTION_BACKOFF_MAX,
    jitter: float = 0.25,
    rng: random.Random | None = None,
) -> Iterator[float]:
    """Exponential backoff with jitter, capped. Yields `retries` delays.

    Jitter matters: four caption workers that all back off by exactly the same
    amount come back as a synchronised burst and get 429'd again together.
    """
    rand = rng or random
    for attempt in range(retries):
        delay = min(base ** (attempt + 1), cap)
        yield delay * (1.0 + rand.uniform(-jitter, jitter))


# ---------------------------------------------------------------------------


def format_selector(fmt: config.OutputFormat) -> str:
    """The yt-dlp format expression for one output format.

    Audio-only requests never take the muxed stream while a DASH audio ladder
    exists — that is what the `bestaudio` prefix guarantees. The `/best`
    fallback only fires when a service offers no audio-only stream at all, and
    the video track is stripped at the ffmpeg stage.
    """
    if fmt.kind == "video":
        return "bestvideo*+bestaudio/best"
    if fmt.kind == "source" and fmt.ext:
        # Strict, deliberately. Asking for FLAC and silently receiving an MP3
        # renamed .flac is worse than an error — it is a lie you only notice
        # after loading it into a session. yt-dlp raises "Requested format is
        # not available", which classifies as Unavailable.
        return f"bestaudio[ext={fmt.ext}]/{fmt.ext}"
    return "bestaudio/best"


def _publish_ffmpeg_location(location: str | None) -> None:
    """Also announce ffmpeg through the contextvar yt-dlp's own CLI sets.

    `--download-sections` is gated on `FFmpegFD.available()`, which is a
    classmethod that builds a bare FFmpegPostProcessor with no downloader
    attached — so it never sees the `ffmpeg_location` in our options dict and
    reads this contextvar instead. yt-dlp's CLI sets it at startup; a library
    caller has to do it by hand or every timed cut fails with "ffmpeg is not
    installed" on a machine that plainly has ffmpeg. yt-dlp's own source
    carries a "Fixme: This may be wrong when --ffmpeg-location is used" note
    above the offending line.
    """
    if not location:
        return
    try:
        from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor

        FFmpegPostProcessor._ffmpeg_location.set(location)
    except (ImportError, AttributeError):  # a future release may fix this
        log.debug("could not publish ffmpeg_location contextvar", exc_info=True)


@dataclass
class Engine:
    cache: Cache | None = None
    cookie_file: Path | None = None
    socket_timeout: float = 30.0
    retries: int = 3
    _rng: random.Random = field(default_factory=random.Random, repr=False)

    # -- option construction --------------------------------------------

    def base_opts(self) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "socket_timeout": self.socket_timeout,
            "retries": self.retries,
            "extractor_retries": self.retries,
            "ignoreerrors": False,
            "nocheckcertificate": False,
        }
        ffmpeg = config.find_ffmpeg()
        if ffmpeg is not None:
            # The full path, not its directory: the only ffmpeg here is a
            # symlink sitting in a bin/ full of unrelated executables, and
            # yt-dlp resolves a directory by looking for exact basenames.
            opts["ffmpeg_location"] = str(ffmpeg)
        if self.cookie_file is not None:
            path = Path(self.cookie_file)
            if path.exists():
                opts["cookiefile"] = str(path)
            else:
                log.warning("cookie file %s does not exist; continuing without", path)
        return opts

    def _ydl(self, extra: dict[str, Any] | None = None):
        from yt_dlp import YoutubeDL

        opts = self.base_opts()
        opts.update(extra or {})
        _publish_ffmpeg_location(opts.get("ffmpeg_location"))
        return YoutubeDL(opts)

    def _call(self, fn, *args, **kwargs):
        """Run a yt-dlp call, retrying only on rate limiting.

        An unavailable video will not become available by asking again, so only
        429s are retried here. Everything else propagates immediately and the
        job queue decides whether the whole job is worth another attempt.
        """
        delays = list(backoff_delays(rng=self._rng))
        last: EngineError | None = None
        for attempt, delay in enumerate([0.0, *delays]):
            if delay:
                time.sleep(delay)
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 — yt-dlp raises broadly
                err = classify(exc)
                if not isinstance(err, RateLimited):
                    raise err from exc
                last = err
                log.warning("rate limited (attempt %s), backing off", attempt + 1)
        raise last or ExtractionFailed("exhausted retries")

    # -- search ----------------------------------------------------------

    def search(
        self, prefix: str, query: str, limit: int = 20, *, use_cache: bool = True
    ) -> list[dict[str, Any]]:
        """Flat search: no player, no format extraction, so it stays fast.

        Flat entries carry id, title, duration and uploader but no format
        ladder — `probe` fills that in for the one result the user picks,
        rather than for all thirty.
        """
        query = query.strip()
        if not query:
            return []
        provider = "youtube" if prefix == YOUTUBE_SEARCH else "soundcloud"

        if use_cache and self.cache is not None:
            hit = self.cache.get_search(provider, query, limit)
            if hit is not None:
                return hit

        def run():
            with self._ydl({"extract_flat": True, "skip_download": True}) as ydl:
                return ydl.extract_info(f"{prefix}{limit}:{query}", download=False)

        info = self._call(run) or {}
        entries = [e for e in (info.get("entries") or []) if e]

        if use_cache and self.cache is not None:
            self.cache.put_search(provider, query, limit, entries)
        return entries

    # -- extract ---------------------------------------------------------

    def extract(
        self, url: str, *, provider: str | None = None, use_cache: bool = True
    ) -> dict[str, Any]:
        """Full extraction, including the format ladder with real bitrates."""
        if use_cache and self.cache is not None and provider:
            hit = self.cache.get_probe(provider, url)
            if hit is not None:
                return hit

        def run():
            with self._ydl({"skip_download": True}) as ydl:
                return ydl.extract_info(url, download=False)

        info = self._call(run)
        if not info:
            raise ExtractionFailed(f"no information returned for {url}")
        # yt-dlp's own sanitiser drops the unserialisable bits so the result
        # can go straight into the sqlite cache as JSON.
        with self._ydl({"skip_download": True}) as ydl:
            info = ydl.sanitize_info(info)

        if use_cache and self.cache is not None and provider:
            self.cache.put_probe(provider, url, info)
        return info

    # -- download --------------------------------------------------------

    def download(
        self,
        url: str,
        dest_dir: Path,
        *,
        selector: str = "bestaudio/best",
        span: tuple[float, float] | None = None,
        progress: ProgressFn | None = None,
        filename_stem: str = "source",
    ) -> Path:
        """Fetch the chosen stream into `dest_dir` untouched.

        No postprocessing happens here — conversion to the requested output
        format is convert.py's job, so that a WAV is produced by one explicit
        ffmpeg command whose arguments are tested, rather than by yt-dlp's
        postprocessor defaults.

        `span` uses --download-sections with keyframes forced at the cuts, so a
        phrase clip transfers only its own seconds rather than the whole video.
        """
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)

        opts: dict[str, Any] = {
            "format": selector,
            "outtmpl": {"default": str(dest_dir / f"{filename_stem}.%(ext)s")},
            "postprocessors": [],
            "overwrites": True,
        }

        if span is not None:
            from yt_dlp.utils import download_range_func

            start, end = span
            opts["download_ranges"] = download_range_func(None, [(start, end)])
            opts["force_keyframes_at_cuts"] = True

        written: list[str] = []

        def hook(status: dict[str, Any]) -> None:
            if status.get("status") == "downloading" and progress is not None:
                total = status.get("total_bytes") or status.get("total_bytes_estimate")
                got = status.get("downloaded_bytes") or 0
                if total:
                    progress(min(got / total, 1.0), "downloading")
            elif status.get("status") == "finished":
                if name := status.get("filename"):
                    written.append(name)
                if progress is not None:
                    progress(1.0, "downloaded")

        opts["progress_hooks"] = [hook]

        def run():
            with self._ydl(opts) as ydl:
                return ydl.extract_info(url, download=True)

        info = self._call(run)

        for candidate in (
            written[-1] if written else None,
            (info or {}).get("requested_downloads", [{}])[0].get("filepath"),
        ):
            if candidate and Path(candidate).exists():
                return Path(candidate)

        # Merged output can land under a name the hook never reported.
        matches = sorted(dest_dir.glob(f"{filename_stem}.*"))
        if matches:
            return matches[0]
        raise ExtractionFailed(f"download produced no file in {dest_dir}")
