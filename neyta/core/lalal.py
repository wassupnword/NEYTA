"""Separation on LALAL.AI's machines instead of this one.

The paid half of the choice offered in Settings. `stems.StemSeparator` runs the
models locally — free, unmetered, nothing leaves the computer. This one uploads
the track, waits, and downloads the stems back. It costs minutes from a plan
and it needs a licence key, so it is never the default and never runs unasked.

The two engines present the same three calls to the window — `available()`,
`supported_options()`, `separate()` — so the export dialog, the job queue and
the stem delivery below them are unchanged by which one is in use.

Where they genuinely differ, they say so rather than pretending:

  * LALAL splits one stem at a time and returns the leftover backing track
    with it, so vocals and instrumental cost one split between them. It has no
    "other" bucket and no lead-versus-backing model, which is why the demucs
    options that promise those are not offered here — see
    config.LALAL_STEM_OPTIONS.
  * progress is real. The API reports a percentage per task, so unlike the
    local runner this does not have to estimate from measured throughput.
  * an upload is a file leaving the machine. That is a fact about this engine
    worth stating in the UI, not a detail to bury.

Protocol: API v0, documented at lalal.ai/api/help. Uploads are a raw body with
the filename in a Content-Disposition header; splits are queued by file id and
polled with /api/check/ until the task reports success.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

import requests

from .. import config
from .stems import StemError

log = logging.getLogger(__name__)

ProgressFn = Callable[[float, str], None]

BASE_URL = "https://www.lalal.ai"

#: Which LALAL stem each of our options asks for, and which of the two tracks
#: that split returns is the thing the user ticked. `back` is the leftover.
@dataclass(frozen=True)
class _Split:
    stem: str
    #: "stem" for the isolated part, "back" for everything else.
    want: str
    dereverb: bool = False
    enhanced: bool = False


SPLITS: dict[str, _Split] = {
    "vocals": _Split(stem="vocals", want="stem"),
    "instrumental": _Split(stem="vocals", want="back"),
    # The same split with LALAL's own cleanup switched on, which is what our
    # "de-reverbed + de-noised" option means locally too.
    "vocals_clean": _Split(
        stem="vocals", want="stem", dereverb=True, enhanced=True
    ),
}

#: How often to ask whether a split is done. The API is polled, not pushed;
#: two seconds is frequent enough to feel live and slow enough to be polite.
POLL_SECONDS = 2.0
#: Give up on a single split after this long. A track that has been "in
#: progress" for half an hour is a failure that has not been reported.
TIMEOUT_SECONDS = 30 * 60


class LalalError(StemError):
    """Anything the service refused or could not finish."""


class LalalUnavailable(LalalError):
    """No licence key, or the key was refused."""


@dataclass
class Limits:
    """What the plan has left, as the billing endpoint reports it."""

    option: str = ""
    email: str = ""
    total: float = 0.0
    used: float = 0.0
    left: float = 0.0

    @property
    def describe(self) -> str:
        if not self.option:
            return "key not recognised"
        return (
            f"{self.option}: {self.left / 60:.0f} of {self.total / 60:.0f} "
            f"minutes left"
        )


@dataclass
class LalalSeparator:
    """The cloud engine, with the same surface as StemSeparator."""

    key: str = "lalal"
    label: str = "LALAL.AI"
    api_key: str = ""
    base_url: str = BASE_URL
    splitter: str = "phoenix"
    session: Any | None = None
    timeout: float = 60.0
    #: Overridden by the test suite so the poll loop does not really sleep.
    sleep: Callable[[float], None] = time.sleep
    poll_seconds: float = POLL_SECONDS
    timeout_seconds: float = TIMEOUT_SECONDS
    _uploads: dict[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.session is None:
            self.session = requests.Session()

    # -- what this engine can do -----------------------------------------

    def available(self) -> bool:
        """A key is the whole installation. There is nothing to build."""
        return bool(self.api_key)

    @property
    def unavailable_note(self) -> str:
        return (
            "LALAL.AI is selected as the stem engine but has no licence key — "
            "add one in Settings, or switch back to UVR."
        )

    def supported_options(self) -> tuple[str, ...]:
        return config.stem_options_for(self.key)

    @property
    def uploads_audio(self) -> bool:
        """True: the track leaves this machine. The UI says so."""
        return True

    # -- HTTP -------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"license {self.api_key}"}

    def _post(self, path: str, **kwargs: Any) -> dict[str, Any]:
        if not self.api_key:
            raise LalalUnavailable(self.unavailable_note)
        url = f"{self.base_url}{path}"
        headers = {**self._headers(), **kwargs.pop("headers", {})}
        try:
            response = self.session.post(
                url, headers=headers, timeout=self.timeout, **kwargs
            )
        except requests.RequestException as exc:
            raise LalalError(f"LALAL.AI did not answer: {exc}") from exc
        if response.status_code in (401, 403):
            raise LalalUnavailable("LALAL.AI refused the licence key")
        if response.status_code != 200:
            raise LalalError(f"LALAL.AI answered {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise LalalError("LALAL.AI returned something that is not JSON") from exc
        if payload.get("status") == "error":
            raise LalalError(payload.get("error") or "unknown error")
        return payload

    def limits(self) -> Limits:
        """Minutes left on the plan. Shown in Settings so the cost of choosing
        this engine is visible before a track is uploaded, not after."""
        if not self.api_key:
            raise LalalUnavailable(self.unavailable_note)
        try:
            response = self.session.get(
                f"{self.base_url}/billing/get-limits/",
                params={"key": self.api_key}, timeout=self.timeout,
            )
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise LalalError(f"could not read the plan: {exc}") from exc
        if payload.get("status") != "success":
            raise LalalUnavailable(payload.get("error") or "key not recognised")
        return Limits(
            option=str(payload.get("option") or ""),
            email=str(payload.get("email") or ""),
            total=float(payload.get("process_duration_limit") or 0.0),
            used=float(payload.get("process_duration_used") or 0.0),
            left=float(payload.get("process_duration_left") or 0.0),
        )

    # -- the three steps --------------------------------------------------

    def upload(self, audio: Path) -> str:
        """Send the file, get the id everything else is keyed by.

        Cached per path for the life of the separator: ticking vocals and
        instrumental is two splits of one upload, and uploading the same track
        twice would cost twice as long to no purpose.
        """
        audio = Path(audio)
        cached = self._uploads.get(str(audio))
        if cached:
            return cached
        if not audio.exists():
            raise LalalError(f"no such file: {audio}")

        with audio.open("rb") as handle:
            payload = self._post(
                "/api/upload/",
                data=handle,
                headers={
                    "Content-Disposition":
                        f'attachment; filename="{audio.name}"',
                },
            )
        file_id = str(payload.get("id") or "")
        if not file_id:
            raise LalalError("the upload finished but no file id came back")
        self._uploads[str(audio)] = file_id
        return file_id

    def queue_split(self, file_id: str, split: _Split) -> None:
        params = [{
            "id": file_id,
            "stem": split.stem,
            "splitter": self.splitter,
            "dereverb_enabled": split.dereverb,
            "enhanced_processing_enabled": split.enhanced,
        }]
        self._post("/api/split/", data={"params": json.dumps(params)})

    def wait(
        self,
        file_id: str,
        *,
        stem: str | None = None,
        progress: ProgressFn | None = None,
        should_cancel: Callable[[], bool] | None = None,
        message: str = "separating",
    ) -> dict[str, Any]:
        """Poll until the split finishes, and hand back its result block.

        `stem` is what was just asked for. A file id carries its most recent
        split, so the first check after queueing a second stem can still be
        showing the first one, finished and successful — a result for the
        wrong stem is treated as not-yet-started rather than as the answer.
        """
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            if should_cancel is not None and should_cancel():
                self.cancel(file_id)
                raise LalalError("cancelled")
            payload = self._post("/api/check/", data={"id": file_id})
            entry = (payload.get("result") or {}).get(file_id) or {}
            task = entry.get("task") or {}
            state = task.get("state")
            split = entry.get("split") or {}
            fresh = stem is None or split.get("stem", stem) == stem

            if state == "error" or entry.get("status") == "error":
                raise LalalError(
                    task.get("error") or entry.get("error") or "split failed"
                )
            if state == "cancelled":
                raise LalalError("cancelled")
            if state == "success" and split and fresh:
                if progress:
                    progress(1.0, message)
                return split

            if progress:
                # Their percentage, not an estimate of it. Held below 1.0 so
                # the bar cannot claim completion before the file is in hand.
                done = float(task.get("progress") or 0.0) / 100.0
                progress(min(max(done, 0.0), 0.99), message)
            if time.monotonic() > deadline:
                raise LalalError(
                    f"LALAL.AI did not finish within "
                    f"{self.timeout_seconds / 60:.0f} minutes"
                )
            self.sleep(self.poll_seconds)

    def cancel(self, file_id: str) -> None:
        """Best effort — a cancel that fails must not mask the cancellation."""
        try:
            self._post("/api/cancel/", data={"id": file_id})
        except LalalError as exc:
            log.debug("cancel failed for %s: %r", file_id, exc)

    def download(self, url: str, dest: Path) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.session.get(url, stream=True, timeout=self.timeout) as r:
                if r.status_code != 200:
                    raise LalalError(f"stem download answered {r.status_code}")
                with dest.open("wb") as out:
                    for chunk in r.iter_content(chunk_size=1 << 16):
                        if chunk:
                            out.write(chunk)
        except requests.RequestException as exc:
            raise LalalError(f"stem download failed: {exc}") from exc
        return dest

    # -- the contract the window uses -------------------------------------

    def separate(
        self,
        audio: Path,
        option_keys: Sequence[str],
        out_dir: Path,
        *,
        audio_seconds: float | None = None,
        output_format: str = "wav",
        progress: ProgressFn | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> dict[str, Path]:
        """Upload once, split for each ticked option, download what was asked.

        Same signature and same return as StemSeparator.separate, including
        that the caller converts to its chosen format afterwards — LALAL hands
        back WAV, which is what the local runner emits too.
        """
        audio = Path(audio)
        out_dir = Path(out_dir)
        wanted = [
            key for key in option_keys
            if key in SPLITS and key in self.supported_options()
        ]
        refused = [
            key for key in option_keys
            if key not in SPLITS and config.stem_option(key).preset is not None
        ]
        if refused:
            # Reached only if the picker let one through; it greys them out.
            raise LalalError(
                "LALAL.AI cannot produce: " + ", ".join(sorted(refused))
            )
        if not wanted:
            return {}

        # One split per distinct model run, not per ticked box. Vocals and
        # instrumental are the two halves of the same split, so ticking both
        # must not be billed twice — the same rule plan_separation applies to
        # the local presets, for the same reason.
        runs: dict[tuple, list[str]] = {}
        for key in wanted:
            split = SPLITS[key]
            runs.setdefault(
                (split.stem, split.dereverb, split.enhanced), []
            ).append(key)

        if progress:
            progress(0.0, "uploading")
        file_id = self.upload(audio)

        collected: dict[str, Path] = {}
        for index, (signature, keys) in enumerate(runs.items()):
            stem = signature[0]
            first = SPLITS[keys[0]]

            def staged(fraction: float, message: str = "", _i=index) -> None:
                if progress:
                    # The upload is the first slice of the bar; each split is
                    # an equal share of the rest.
                    progress(0.1 + 0.9 * (_i + fraction) / len(runs), message)

            self.queue_split(file_id, first)
            result = self.wait(
                file_id, stem=stem, progress=staged,
                should_cancel=should_cancel,
                message="separating — " + ", ".join(
                    config.stem_option(k).label.lower() for k in keys
                ),
            )
            for key in keys:
                want = SPLITS[key].want
                url = result.get(
                    "stem_track" if want == "stem" else "back_track"
                )
                if not url:
                    raise LalalError(f"no {want} track came back for {key}")
                collected[key] = self.download(
                    url, out_dir / f"{audio.stem}_{key}.{output_format}"
                )
        return collected
