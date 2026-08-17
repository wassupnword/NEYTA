"""The UVR driver, and the calibration that makes its time estimates honest.

NEYTA never imports torch. It runs uvr_runner.py inside uvr-local's
interpreter and reads JSON off its stdout (build plan 3.3), so the two
dependency trees never have to agree about anything.

On time estimates: audio-separator does not report progress in any form this
side of the boundary can read, so a progress bar here would be a lie dressed
as information. Instead the first separation of each preset is timed, the
measured throughput is stored, and every later estimate comes from what this
machine actually did rather than from a number someone guessed. Until a preset
has been run once, the estimate says so.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .. import config
from . import naming

log = logging.getLogger(__name__)

ProgressFn = Callable[[float, str], None]

RUNNER = Path(__file__).with_name("uvr_runner.py")

#: Presets whose checkpoints are not in uvr-local/models/ yet. The first run
#: of one of these downloads a few hundred MB before any audio is processed,
#: which is worth saying out loud rather than looking like a hang.
_MODEL_FILES = {
    "vocals": ("model_bs_roformer_ep_317_sdr_12.9755.ckpt",),
    "vocals_fast": ("UVR-MDX-NET-Voc_FT.onnx",),
    "vocals_clean": (
        "model_bs_roformer_ep_317_sdr_12.9755.ckpt",
        "UVR-DeEcho-DeReverb.pth", "UVR-DeNoise.pth",
    ),
    "karaoke": (
        "model_bs_roformer_ep_317_sdr_12.9755.ckpt", "6_HP-Karaoke-UVR.pth",
    ),
    "stems": ("htdemucs_ft.yaml",),
    "stems6": ("htdemucs_6s.yaml",),
    "dereverb": ("UVR-DeEcho-DeReverb.pth",),
    "denoise": ("UVR-DeNoise.pth",),
}


class StemError(RuntimeError):
    pass


class UvrUnavailable(StemError):
    pass


@dataclass(frozen=True)
class StemResult:
    preset: str
    stems: dict[str, Path]
    elapsed: float
    audio_seconds: float | None = None

    @property
    def rate(self) -> float | None:
        """Seconds of compute per second of audio."""
        if not self.audio_seconds:
            return None
        return self.elapsed / self.audio_seconds


# ---------------------------------------------------------------------------
# What to run, and what to keep
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SeparationStep:
    preset: str
    #: Stem names to keep, or None for all of them.
    wanted: frozenset[str] | None

    def keep(self, produced: dict[str, Path]) -> dict[str, Path]:
        if self.wanted is None:
            return dict(produced)
        return {k: v for k, v in produced.items() if k in self.wanted}


def plan_separation(option_keys: Iterable[str]) -> list[SeparationStep]:
    """Turn ticked checkboxes into the least work that satisfies them.

    "vocals" and "instrumental" are two checkboxes over one BS-Roformer run,
    so ticking both must not run the model twice — and ticking only one must
    still not hand back the other, because the plan promises you get exactly
    what you ticked.
    """
    wanted: dict[str, set[str] | None] = {}
    for key in option_keys:
        option = config.stem_option(key)
        if option.preset is None:
            continue  # "original" runs no model
        if option.preset not in wanted:
            wanted[option.preset] = set() if option.stems else None
        current = wanted[option.preset]
        if option.stems and current is not None:
            current.update(option.stems)
        else:
            wanted[option.preset] = None  # an "all stems" option subsumes the rest

    return [
        SeparationStep(preset, frozenset(names) if names is not None else None)
        for preset, names in wanted.items()
    ]


def missing_models(preset: str) -> list[str]:
    """Checkpoints this preset needs that are not on disk yet."""
    models_dir = config.UVR_ROOT / "models"
    return [
        name for name in _MODEL_FILES.get(preset, ())
        if not (models_dir / name).exists()
    ]


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------


@dataclass
class Calibration:
    """Measured throughput, per preset, on this machine.

    Stored as an exponential moving average so one unusually loaded run does
    not poison the estimate, and one unusually fast one does not either.
    """

    path: Path | None = None
    smoothing: float = 0.4
    rates: dict[str, float] = field(default_factory=dict)
    samples: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text("utf-8"))
                self.rates = {k: float(v) for k, v in data.get("rates", {}).items()}
                self.samples = {k: int(v) for k, v in data.get("samples", {}).items()}
            except (ValueError, OSError):
                log.warning("unreadable calibration at %s; starting fresh", self.path)

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"rates": self.rates, "samples": self.samples}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), "utf-8")
        tmp.replace(self.path)

    def record(self, preset: str, audio_seconds: float, elapsed: float) -> None:
        if audio_seconds <= 0 or elapsed <= 0:
            return
        rate = elapsed / audio_seconds
        previous = self.rates.get(preset)
        self.rates[preset] = (
            rate if previous is None
            else previous * (1 - self.smoothing) + rate * self.smoothing
        )
        self.samples[preset] = self.samples.get(preset, 0) + 1
        self.save()

    def rate(self, preset: str) -> float | None:
        return self.rates.get(preset)

    def estimate(self, preset: str, audio_seconds: float) -> float | None:
        """Seconds this preset will take, or None if never measured here."""
        rate = self.rates.get(preset)
        return None if rate is None else rate * audio_seconds

    def estimate_all(
        self, steps: Sequence[SeparationStep], audio_seconds: float
    ) -> tuple[float | None, bool]:
        """(seconds, complete). `complete` is False when any preset in the set
        has never been run here, so the UI can say "at least" rather than
        implying a precision it does not have."""
        total = 0.0
        complete = True
        for step in steps:
            seconds = self.estimate(step.preset, audio_seconds)
            if seconds is None:
                complete = False
            else:
                total += seconds
        return (total if total else None), complete

    def describe(self, steps: Sequence[SeparationStep], audio_seconds: float) -> str:
        if not steps:
            return "no separation — the original file, instantly"
        if not audio_seconds:
            return "estimate needs the track length"
        seconds, complete = self.estimate_all(steps, audio_seconds)
        if seconds is None:
            return "first run on this machine — timing it to learn how fast it is"
        prefix = "" if complete else "at least "
        return f"{prefix}{_human(seconds)} on this machine"


def _human(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, secs = divmod(int(seconds + 0.5), 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


# ---------------------------------------------------------------------------
# The driver
# ---------------------------------------------------------------------------


class StemSeparator:
    #: Names this engine by, for the settings page and for messages. The cloud
    #: engine in lalal.py carries the same two, so nothing above has to ask
    #: which class it is holding.
    key = "uvr"
    label = "UVR"

    def __init__(
        self,
        python: Path | None = None,
        uvr_root: Path | None = None,
        calibration: Calibration | None = None,
        runner: Path | None = None,
    ) -> None:
        self.python = Path(python or config.UVR_PYTHON)
        self.uvr_root = Path(uvr_root or config.UVR_ROOT)
        self.calibration = calibration or Calibration()
        #: Overridden by the test suite with a stub that speaks the same JSON
        #: protocol, so the driver is exercised without loading torch.
        self.runner = Path(runner or RUNNER)

    def available(self) -> bool:
        return self.python.exists() and (self.uvr_root / "uvr.py").exists()

    @property
    def unavailable_note(self) -> str:
        return "uvr-local is not built. Run tools/setup.sh."

    def supported_options(self) -> tuple[str, ...]:
        """Every option there is. The local engine is the one that can do all
        of them, which is part of why it stays the default."""
        return config.stem_options_for(self.key)

    @property
    def uploads_audio(self) -> bool:
        """False: nothing leaves this machine."""
        return False

    def _require(self) -> None:
        if not self.available():
            raise UvrUnavailable(
                f"uvr-local is not built at {self.uvr_root}. Run tools/setup.sh."
            )

    # -- one preset -------------------------------------------------------

    def run_preset(
        self,
        audio: Path,
        preset: str,
        out_dir: Path,
        *,
        audio_seconds: float | None = None,
        output_format: str = "wav",
        progress: ProgressFn | None = None,
        should_cancel: Callable[[], bool] | None = None,
        poll: float = 0.5,
    ) -> StemResult:
        """Separate one file with one preset.

        Progress is time-based against this machine's measured throughput, and
        holds at 99% rather than claiming completion it cannot verify.
        """
        self._require()
        audio = Path(audio)
        if not audio.exists():
            raise StemError(f"no such file: {audio}")
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        estimate = (
            self.calibration.estimate(preset, audio_seconds)
            if audio_seconds else None
        )

        command = [
            str(self.python), str(self.runner),
            "--uvr-root", str(self.uvr_root),
            "--input", str(audio),
            "--output-dir", str(out_dir),
            "--preset", preset,
            "--format", output_format,
            "--model-dir", str(self.uvr_root / "models"),
        ]

        if progress:
            pending = missing_models(preset)
            progress(0.0, "downloading models" if pending else "loading model")

        started = time.monotonic()
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
            # Its own group, so cancelling kills torch's worker threads too
            # rather than orphaning them.
            start_new_session=True,
        )

        result: StemResult | None = None
        error: str | None = None
        cancelled = False

        # Reader threads rather than non-blocking reads: a non-blocking read
        # on a text-mode pipe hands the decoder None and raises. Draining
        # stderr matters too — audio-separator is chatty, and a full stderr
        # pipe deadlocks the child while the parent waits for stdout.
        lines: "queue.Queue[str | None]" = queue.Queue()
        stderr_lines: list[str] = []
        readers = [
            threading.Thread(target=_pump, args=(process.stdout, lines), daemon=True),
            threading.Thread(
                target=_collect, args=(process.stderr, stderr_lines), daemon=True
            ),
        ]
        for reader in readers:
            reader.start()

        try:
            finished = False
            while not finished:
                if should_cancel is not None and should_cancel():
                    cancelled = True
                    break
                try:
                    line = lines.get(timeout=poll)
                except queue.Empty:
                    line = ""
                    if process.poll() is not None:
                        finished = True
                if line is None:  # stdout closed
                    finished = True
                elif line:
                    event = _parse(line)
                    kind = event.get("event") if event else None
                    if kind == "done":
                        result = StemResult(
                            preset=event["preset"],
                            stems={k: Path(v) for k, v in event["stems"].items()},
                            elapsed=float(event.get("elapsed", 0.0)),
                            audio_seconds=audio_seconds,
                        )
                    elif kind == "error":
                        error = event.get("message", "unknown error")
                    elif kind == "start" and progress:
                        progress(0.02, "separating")

                if progress and not finished:
                    elapsed = time.monotonic() - started
                    if estimate:
                        progress(min(elapsed / estimate, 0.99), "separating")
                    else:
                        progress(0.5, f"separating — timing this run ({elapsed:.0f}s)")
        finally:
            if cancelled:
                _terminate(process)
            process.wait()
            for reader in readers:
                reader.join(timeout=2)
            for pipe in (process.stdout, process.stderr):
                if pipe is not None and not pipe.closed:
                    pipe.close()
        stderr_tail = "".join(stderr_lines)[-800:]

        if cancelled:
            raise StemError("cancelled")
        if error:
            raise StemError(f"{preset}: {error}")
        if result is None:
            raise StemError(
                f"{preset}: separation produced no result "
                f"(exit {process.returncode})\n{stderr_tail}"
            )

        if audio_seconds:
            self.calibration.record(preset, audio_seconds, result.elapsed)
        if progress:
            progress(1.0, "separated")
        return result

    # -- a whole ticked selection ----------------------------------------

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
        """Run every preset the ticked options need and return the stems they
        asked for, as {stem_name: path}.

        Presets run one at a time on purpose: each already saturates the CPU,
        and running two at once on an M-series machine makes both slower while
        making the time estimate meaningless.
        """
        steps = plan_separation(option_keys)
        if not steps:
            return {}

        collected: dict[str, Path] = {}
        for index, step in enumerate(steps):
            def staged(fraction: float, message: str = "", _i=index) -> None:
                if progress:
                    progress((_i + fraction) / len(steps), message)

            result = self.run_preset(
                audio, step.preset, Path(out_dir) / step.preset,
                audio_seconds=audio_seconds,
                output_format=output_format,
                progress=staged,
                should_cancel=should_cancel,
            )
            for name, path in step.keep(result.stems).items():
                # Two presets can both emit "vocals"; keep both under
                # distinguishable names rather than silently dropping one.
                key = name if name not in collected else f"{step.preset}_{name}"
                collected[key] = path
        return collected


def separator_for(settings, calibration: Calibration | None = None):
    """The separator the settings ask for.

    Built fresh rather than cached, because the answer changes the moment the
    engine is switched or a licence key is pasted, and a separation that runs
    on the engine you have just stopped choosing is a surprise.

    Falls back to the local engine on its own if the chosen one has no key —
    `Settings.stem_engine` already does that; this just builds what it says.
    """
    option = settings.stem_engine
    if option.key == "lalal":
        # Imported here: lalal builds on StemError from this module, so at
        # module level the two would import each other.
        from .lalal import LalalSeparator

        return LalalSeparator(api_key=settings.engine_key(option) or "")
    return StemSeparator(calibration=calibration)


def _pump(pipe, sink: "queue.Queue[str | None]") -> None:
    """Blocking readline in a thread; None marks end of stream."""
    try:
        for line in pipe:
            sink.put(line)
    except (ValueError, OSError):
        pass
    finally:
        sink.put(None)


def _collect(pipe, sink: list[str]) -> None:
    try:
        for line in pipe:
            sink.append(line)
    except (ValueError, OSError):
        pass


def _parse(line: str) -> dict | None:
    line = line.strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except ValueError:
        log.debug("non-JSON line from runner: %s", line[:200])
        return None


def _terminate(process: subprocess.Popen) -> None:
    """Stop the whole process group, then insist."""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


# ---------------------------------------------------------------------------
# Naming the output
# ---------------------------------------------------------------------------


def deliver(
    stems: dict[str, Path],
    dest_dir: Path,
    *,
    title: str,
    artist: str | None = None,
    ext: str = "wav",
) -> dict[str, Path]:
    """Move raw stems into the download folder as "Artist - Title [stem].ext".

    Same sanitisation and the same -2/-3 collision rule as everything else, so
    a separation can never overwrite a previous one.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    delivered: dict[str, Path] = {}
    for name, source in sorted(stems.items()):
        if not Path(source).exists():
            log.warning("stem %s missing at %s", name, source)
            continue
        target = naming.resolve_output(
            dest_dir, title=title, artist=artist, stem=name,
            ext=Path(source).suffix.lstrip(".") or ext,
        )
        Path(source).replace(target)
        delivered[name] = target
    return delivered
