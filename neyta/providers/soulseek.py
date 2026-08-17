"""The Soulseek tab.

The only tab whose source can genuinely exceed everything the others offer —
peers share whatever they have, which is often real FLAC and true 320. It is
also the only one that cannot be previewed without transferring, because
Soulseek is peer-to-peer file transfer and there is no stream to scrub. The
button says "Fetch & preview" so that difference is visible rather than
discovered.

Three more things this tab does differently, all forced by the protocol:

  * searches take 15-30 seconds by design, and results arrive from peers as
    they answer rather than all at once;
  * a result is a file on someone's machine, so free-slot state matters as
    much as bitrate;
  * peers vanish mid-transfer routinely, so a failed download retries against
    the next-best copy of the same file instead of reporting failure.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

from .. import config
from ..core import convert
from .base import (
    LocalFile, Media, NotSupported, Preview, ProgressFn, Provider, Result,
    Stream,
)

log = logging.getLogger(__name__)

#: soulseek_api lives beside NEYTA rather than in it — it is a standalone
#: package with its own tests and its own stub server.
if str(config.SOULSEEK_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(config.SOULSEEK_PKG_ROOT))

AUDIO_EXTENSIONS = ("flac", "wav", "aiff", "alac", "ape", "wv",
                    "mp3", "m4a", "aac", "ogg", "opus")

LOSSLESS_EXTENSIONS = ("flac", "wav", "aiff", "alac", "ape", "wv")


class SoulseekUnavailable(RuntimeError):
    """No daemon, no login, or the network is not reachable."""


def _import_client():
    try:
        from soulseek_api import SoulseekClient  # type: ignore
        from soulseek_api import errors as sk_errors  # type: ignore
    except ImportError as exc:  # pragma: no cover - packaging failure
        raise SoulseekUnavailable(
            f"soulseek_api is not importable from {config.SOULSEEK_PKG_ROOT}: {exc}"
        ) from exc
    return SoulseekClient, sk_errors


def _display_name(filename: str) -> str:
    """Peers share Windows paths; the last component is the track."""
    cleaned = filename.replace("\\", "/")
    return Path(cleaned).name or cleaned


def _artist_from_path(filename: str) -> str | None:
    """Guess the artist from the folder a peer filed the track under.

    Soulseek carries no tags in search results, only paths. "Artist/Album/01
    Track.flac" is the overwhelmingly common shape, so the grandparent
    directory is the best guess available — and it is a guess, which is why it
    yields to anything better downstream.
    """
    parts = [p for p in filename.replace("\\", "/").split("/") if p]
    return parts[-3] if len(parts) >= 3 else None


class SoulseekProvider(Provider):
    key = "soulseek"
    label = "Soulseek"
    #: Whole files from a peer; there is no way to ask for part of one.
    supports_spans = False

    def __init__(
        self,
        client: Any | None = None,
        bootstrap: Any | None = None,
        search_timeout: float = config.SOULSEEK_SEARCH_TIMEOUT,
    ) -> None:
        self._client = client
        self.bootstrap = bootstrap
        self.search_timeout = search_timeout

    # -- connection -------------------------------------------------------

    @property
    def client(self):
        if self._client is None:
            raise SoulseekUnavailable(
                "the Soulseek daemon is not connected — add your login in "
                "Settings and start it there"
            )
        return self._client

    def connect(self, url: str, api_key: str):
        SoulseekClient, _ = _import_client()
        self._client = SoulseekClient(url=url, api_key=api_key)
        return self._client

    def connected(self) -> bool:
        if self._client is None:
            return False
        try:
            return bool(self._client.ping())
        except Exception:  # noqa: BLE001 — a dead daemon is not an exception here
            return False

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            finally:
                self._client = None

    # -- mapping ----------------------------------------------------------

    def to_result(self, file: Any) -> Result:
        extension = (getattr(file, "extension", "") or "").lower().lstrip(".")
        if not extension:
            extension = Path(_display_name(file.filename)).suffix.lstrip(".").lower()
        bitrate = float(getattr(file, "bitrate", 0) or 0) or None
        length = float(getattr(file, "length", 0) or 0) or None
        free = bool(getattr(file, "free_upload_slot", False))

        return Result(
            provider=self.key,
            # A file is identified by who has it and where, not by any id the
            # network assigns — there isn't one.
            id=f"{file.username}:{file.filename}",
            title=_display_name(file.filename),
            artist=_artist_from_path(file.filename),
            duration=length,
            url=None,
            source_kbps=bitrate,
            availability="free" if free else "queued",
            extra={
                "username": file.username,
                "filename": file.filename,
                "size": getattr(file, "size", 0),
                "extension": extension,
                "queue_length": getattr(file, "queue_length", 0),
                "upload_speed": getattr(file, "upload_speed", 0),
                "free_slot": free,
                "lossless": extension in LOSSLESS_EXTENSIONS,
            },
        )

    # -- contract ---------------------------------------------------------

    def search(self, query: str, limit: int = 40) -> list[Result]:
        """Ask the network. This takes 15-30s by protocol design.

        Ranked free-slot-first: a file behind a long queue may take hours, and
        one from a peer with an open slot starts now.
        """
        query = query.strip()
        if not query:
            return []
        # Touch the client first: "add your login in Settings" is a more
        # useful thing to be told than an ImportError, and not being
        # connected is by far the likelier reason to be here.
        client = self.client
        _, sk_errors = _import_client()
        try:
            files = client.search(
                query, timeout=self.search_timeout, audio_only=True,
                file_limit=max(limit * 4, 200),
            )
        except sk_errors.SearchTimeout:
            return []
        except sk_errors.SoulseekError as exc:
            raise SoulseekUnavailable(str(exc)) from exc

        results = [self.to_result(f) for f in files]
        results.sort(key=self._rank, reverse=True)
        return results[:limit]

    @staticmethod
    def _rank(result: Result) -> tuple:
        extra = result.extra
        return (
            1 if extra.get("free_slot") else 0,
            1 if extra.get("lossless") else 0,
            result.source_kbps or 0,
            extra.get("upload_speed", 0),
        )

    def probe(self, result: Result) -> Media:
        """What the peer has. There is no ladder — a Soulseek result is one
        file, and its properties are already known from the search."""
        extra = result.extra
        extension = extra.get("extension") or ""
        stream = Stream(
            id=extra.get("username", "peer"),
            ext=extension,
            bitrate_kbps=result.source_kbps,
            codec=extension or None,
            filesize=extra.get("size"),
        )
        return Media(result=result, streams=(stream,), lossless=stream.lossless)

    def fetch(
        self,
        result: Result,
        fmt: config.OutputFormat,
        dest: Path,
        *,
        progress: ProgressFn | None = None,
        span: tuple[float, float] | None = None,
        alternatives: Sequence[Result] = (),
    ) -> Path:
        """Transfer the file, then convert if a conversion was asked for.

        `alternatives` are other copies of the same file from other peers.
        Peers go offline mid-transfer as a matter of course, so a failure
        moves to the next-best copy rather than surfacing as an error.
        """
        if span is not None:
            raise NotSupported(
                "Soulseek transfers whole files; there is no way to request "
                "part of one from a peer"
            )

        candidates = [result, *alternatives]
        last_error: Exception | None = None

        for index, candidate in enumerate(candidates):
            try:
                transferred = self._transfer(candidate, progress)
            except Exception as exc:  # noqa: BLE001 — try the next peer
                last_error = exc
                log.warning(
                    "transfer from %s failed (%s)%s",
                    candidate.extra.get("username"), exc,
                    "; trying the next peer" if index + 1 < len(candidates) else "",
                )
                continue

            return self._deliver(transferred, dest, fmt, progress)

        raise SoulseekUnavailable(
            f"no peer would send this file ({last_error})"
        )

    def _transfer(self, result: Result, progress: ProgressFn | None) -> Path:
        _, sk_errors = _import_client()
        extra = result.extra

        def on_progress(transfer) -> None:
            if progress is None:
                return
            size = getattr(transfer, "size", 0) or extra.get("size") or 0
            done = getattr(transfer, "bytes_transferred", 0) or 0
            if size:
                progress(min(done / size, 1.0) * 0.8, "transferring")

        file = _FileRef(
            username=extra["username"],
            filename=extra["filename"],
            size=extra.get("size", 0),
        )
        transfer = self.client.download_and_wait(file, on_progress=on_progress)
        path = _transferred_path(transfer, self.client)
        if path is None or not Path(path).exists():
            raise SoulseekUnavailable("the transfer finished but no file arrived")
        return Path(path)

    def _deliver(
        self,
        source: Path,
        dest: Path,
        fmt: config.OutputFormat,
        progress: ProgressFn | None,
    ) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)

        got = source.suffix.lstrip(".").lower()
        if fmt.kind == "source" and (not fmt.ext or got == fmt.ext.lower()):
            # Passthrough: the peer's file, exactly as they have it. This is
            # the whole reason to use this tab.
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
        """Fetch-then-play. There is no stream to scrub on a P2P network, so
        auditioning means transferring first — which is why the button says
        "Fetch & preview" rather than "Preview"."""
        temp = self.paths_preview() / f"{result.id.replace('/', '_')[:80]}"
        transferred = self._transfer(result, None)
        target = temp.with_suffix(transferred.suffix)
        target.parent.mkdir(parents=True, exist_ok=True)
        transferred.replace(target)
        return LocalFile(path=target, temporary=True)

    @property
    def preview_label(self) -> str:
        return "Fetch & preview"

    @property
    def preview_requires_transfer(self) -> bool:
        return True

    def paths_preview(self) -> Path:
        return config.Paths.default().preview_dir


class _FileRef:
    """The shape `soulseek_api.download` expects, built from a Result's extra.

    A Result travels through the job queue and the UI, so it cannot carry the
    library's own dataclass without dragging that import everywhere.
    """

    def __init__(self, username: str, filename: str, size: int) -> None:
        self.username = username
        self.filename = filename
        self.size = size


def _transferred_path(transfer: Any, client: Any) -> str | None:
    """Where the completed transfer landed.

    slskd reports this differently between versions, so several shapes are
    tried before giving up rather than assuming one.
    """
    raw = getattr(transfer, "raw", None) or {}
    for key in ("localPath", "destinationPath", "path", "filename"):
        value = raw.get(key)
        if value and Path(value).exists():
            return value
    filename = getattr(transfer, "filename", "")
    if filename:
        candidate = Path(filename.replace("\\", "/"))
        downloads = getattr(client, "download_dir", None)
        if downloads:
            guess = Path(downloads) / candidate.name
            if guess.exists():
                return str(guess)
    return None
