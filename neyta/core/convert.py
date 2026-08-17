"""ffmpeg: transcode, trim, probe, silence-detect.

There is no ffprobe on this machine — imageio-ffmpeg ships the ffmpeg binary
alone — so media info is parsed out of ffmpeg's own stderr banner. That is the
only place in this module doing anything unusual, and it is covered by tests
against a checked-in fixture so a format change in a future ffmpeg is loud.

The argument builders are pure functions, separately tested, so the mapping
from an OutputFormat to a command line can be checked without running anything.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .. import config

ProgressFn = Callable[[float, str], None]


class ConversionError(RuntimeError):
    pass


class FfmpegMissing(ConversionError):
    pass


@dataclass(frozen=True)
class AudioInfo:
    duration: float | None = None
    sample_rate: int | None = None
    channels: int | None = None
    bitrate_kbps: float | None = None
    codec: str | None = None

    @property
    def is_stereo(self) -> bool:
        return self.channels == 2


@dataclass(frozen=True)
class Span:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def ffmpeg_binary(override: Path | str | None = None) -> Path:
    if override is not None:
        return Path(override)
    found = config.find_ffmpeg()
    if found is None:
        raise FfmpegMissing(
            "no ffmpeg — expected the static arm64 binary at "
            "uvr-local/.venv/bin/ffmpeg. Run tools/setup.sh."
        )
    return found


# ---------------------------------------------------------------------------
# Argument builders (pure)
# ---------------------------------------------------------------------------

#: Sample formats per output. 24-bit is the Ableton default the plan asks for.
_PCM_CODEC = {(48000, 24): "pcm_s24le", (44100, 16): "pcm_s16le"}


def codec_args(fmt: config.OutputFormat) -> list[str]:
    """The encoder half of the command line for one output format."""
    if fmt.kind == "video":
        return ["-c", "copy"]

    if fmt.kind == "source":
        # Byte-for-byte where the container allows it. This is the whole point
        # of the Soulseek passthrough and of "M4A (source stream)".
        return ["-vn", "-c:a", "copy"]

    if fmt.kind == "pcm":
        codec = _PCM_CODEC.get((fmt.sample_rate or 0, fmt.bit_depth or 0))
        if codec is None:
            raise ConversionError(
                f"no PCM encoder for {fmt.sample_rate}Hz/{fmt.bit_depth}-bit"
            )
        args = ["-vn", "-c:a", codec]
        if fmt.sample_rate:
            args += ["-ar", str(fmt.sample_rate)]
        # Channel count is deliberately not forced. Upmixing a mono source to
        # stereo doubles the file and adds nothing, which is the same argument
        # that makes MP3 320 from a 128k source an upscale.
        return args

    if fmt.kind == "lossy":
        if fmt.ext == "mp3":
            return ["-vn", "-c:a", "libmp3lame", "-b:a", f"{fmt.bitrate_kbps}k"]
        if fmt.ext in ("m4a", "aac"):
            return ["-vn", "-c:a", "aac", "-b:a", f"{fmt.bitrate_kbps}k"]
        raise ConversionError(f"no encoder mapped for .{fmt.ext}")

    raise ConversionError(f"unknown format kind {fmt.kind!r}")


def trim_args(start: float | None, end: float | None) -> list[str]:
    """Input-side seek plus an explicit duration.

    `-ss` before the input is the fast seek and, when re-encoding, is still
    frame-accurate. `-t` is used rather than `-to` because `-to`'s meaning
    relative to a preceding `-ss` has changed between ffmpeg releases; a
    duration is unambiguous in every version.
    """
    args: list[str] = []
    if start:
        args += ["-ss", f"{start:.3f}"]
    if end is not None:
        duration = end - (start or 0.0)
        if duration <= 0:
            raise ConversionError(f"empty span: {start} -> {end}")
        args += ["-t", f"{duration:.3f}"]
    return args


def build_command(
    src: Path,
    dest: Path,
    fmt: config.OutputFormat,
    *,
    start: float | None = None,
    end: float | None = None,
    ffmpeg: Path | str | None = None,
    extra: Sequence[str] = (),
) -> list[str]:
    binary = ffmpeg_binary(ffmpeg)
    return [
        str(binary), "-hide_banner", "-nostdin", "-y",
        *trim_args(start, end),
        "-i", str(src),
        *codec_args(fmt),
        *extra,
        "-progress", "pipe:1", "-nostats",
        str(dest),
    ]


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)")
_AUDIO_RE = re.compile(
    r"Stream #\d+:\d+.*?: Audio:\s*([A-Za-z0-9_]+).*?,\s*(\d+)\s*Hz,\s*([^,]+)"
)
_BITRATE_RE = re.compile(r"bitrate:\s*(\d+)\s*kb/s")
_STREAM_BITRATE_RE = re.compile(r"Audio:.*?,\s*(\d+)\s*kb/s")

_CHANNEL_COUNT = {"mono": 1, "stereo": 2, "5.1": 6, "5.1(side)": 6, "7.1": 8}


def parse_banner(stderr: str) -> AudioInfo:
    """Pull duration, sample rate, channels, codec and bitrate out of the
    banner ffmpeg prints for its input. Split out from `probe` so it can be
    tested against captured text with no subprocess."""
    duration = None
    if m := _DURATION_RE.search(stderr):
        h, mnt, s = m.groups()
        duration = int(h) * 3600 + int(mnt) * 60 + float(s)

    codec = sample_rate = channels = None
    if m := _AUDIO_RE.search(stderr):
        codec, rate, layout = m.groups()
        sample_rate = int(rate)
        layout = layout.strip()
        channels = _CHANNEL_COUNT.get(layout)
        if channels is None and (cm := re.match(r"(\d+)\s*channels?", layout)):
            channels = int(cm.group(1))

    bitrate = None
    if m := _STREAM_BITRATE_RE.search(stderr):
        bitrate = float(m.group(1))
    elif m := _BITRATE_RE.search(stderr):
        bitrate = float(m.group(1))

    return AudioInfo(
        duration=duration,
        sample_rate=sample_rate,
        channels=channels,
        bitrate_kbps=bitrate,
        codec=codec,
    )


def probe(path: Path | str, *, ffmpeg: Path | str | None = None) -> AudioInfo:
    """What is actually in this file. Used to show true bitrates and to assert
    that a finished WAV really is what was asked for."""
    binary = ffmpeg_binary(ffmpeg)
    path = Path(path)
    if not path.exists():
        raise ConversionError(f"no such file: {path}")
    # ffmpeg with no output exits 1 after printing the banner. That is the
    # documented way to get file info out of it and is not an error here.
    proc = subprocess.run(
        [str(binary), "-hide_banner", "-nostdin", "-i", str(path)],
        capture_output=True, text=True, timeout=120,
    )
    info = parse_banner(proc.stderr)
    if info.duration is None and info.codec is None:
        raise ConversionError(f"ffmpeg could not read {path}:\n{proc.stderr[-500:]}")
    return info


# ---------------------------------------------------------------------------
# Transcoding
# ---------------------------------------------------------------------------

_OUT_TIME_RE = re.compile(r"out_time_us=(\d+)")


def transcode(
    src: Path | str,
    dest: Path | str,
    fmt: config.OutputFormat,
    *,
    start: float | None = None,
    end: float | None = None,
    ffmpeg: Path | str | None = None,
    progress: ProgressFn | None = None,
    total_duration: float | None = None,
    timeout: float = 3600,
) -> Path:
    """Convert `src` to `dest` in `fmt`, optionally cutting to [start, end].

    Returns the path written. Raises ConversionError with ffmpeg's own tail on
    failure rather than a generic non-zero-exit message.
    """
    src, dest = Path(src), Path(dest)
    if not src.exists():
        raise ConversionError(f"no such file: {src}")
    dest.parent.mkdir(parents=True, exist_ok=True)

    if progress is not None and total_duration is None:
        try:
            total_duration = probe(src, ffmpeg=ffmpeg).duration
        except ConversionError:
            total_duration = None
    if total_duration and (start is not None or end is not None):
        span_end = end if end is not None else total_duration
        total_duration = max(0.0, span_end - (start or 0.0))

    cmd = build_command(src, dest, fmt, start=start, end=end, ffmpeg=ffmpeg)

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1
    )
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            if progress is None or not total_duration:
                continue
            if m := _OUT_TIME_RE.search(line):
                done = int(m.group(1)) / 1_000_000
                progress(min(done / total_duration, 1.0), "converting")
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise ConversionError(f"ffmpeg timed out after {timeout}s") from None
    finally:
        stderr = proc.stderr.read() if proc.stderr else ""
        proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()

    if proc.returncode != 0:
        raise ConversionError(
            f"ffmpeg exited {proc.returncode}\n"
            f"  command: {' '.join(cmd)}\n"
            f"  {stderr.strip()[-800:]}"
        )
    if not dest.exists() or dest.stat().st_size == 0:
        raise ConversionError(f"ffmpeg reported success but {dest} is empty")

    if progress is not None:
        progress(1.0, "converted")
    return dest


# ---------------------------------------------------------------------------
# Silence detection (phase 5 auto-trim; the primitive lives here)
# ---------------------------------------------------------------------------

_SILENCE_START_RE = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*([\d.]+)")


def parse_silence(stderr: str, duration: float | None = None) -> list[Span]:
    """Turn silencedetect's log lines into spans.

    A trailing `silence_start` with no matching `silence_end` means the file
    ends in silence; that span is closed at the file's duration when known.
    """
    spans: list[Span] = []
    pending: float | None = None
    for line in stderr.splitlines():
        if m := _SILENCE_START_RE.search(line):
            pending = max(0.0, float(m.group(1)))
        elif m := _SILENCE_END_RE.search(line):
            if pending is not None:
                spans.append(Span(pending, float(m.group(1))))
                pending = None
    if pending is not None and duration is not None:
        spans.append(Span(pending, duration))
    return spans


def detect_silence(
    path: Path | str,
    *,
    threshold_db: float = -40.0,
    min_silence: float = 0.25,
    ffmpeg: Path | str | None = None,
) -> list[Span]:
    binary = ffmpeg_binary(ffmpeg)
    proc = subprocess.run(
        [str(binary), "-hide_banner", "-nostdin", "-i", str(path),
         "-af", f"silencedetect=noise={threshold_db}dB:d={min_silence}",
         "-f", "null", "-"],
        capture_output=True, text=True, timeout=600,
    )
    info = parse_banner(proc.stderr)
    return parse_silence(proc.stderr, info.duration)


def peaks(
    path: Path | str,
    buckets: int = 600,
    *,
    ffmpeg: Path | str | None = None,
    rate: int = 8000,
) -> list[float]:
    """Amplitude per horizontal pixel, 0..1, for drawing a waveform.

    Decoded to mono 8kHz s16le and reduced to peaks here rather than mean
    levels: a transient one sample wide is exactly what you are looking for
    when placing a cut, and averaging erases it.
    """
    binary = ffmpeg_binary(ffmpeg)
    proc = subprocess.run(
        [str(binary), "-hide_banner", "-nostdin", "-v", "quiet",
         "-i", str(path), "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"],
        capture_output=True, timeout=600,
    )
    raw = proc.stdout
    if not raw:
        return []

    import array

    samples = array.array("h")
    samples.frombytes(raw[: len(raw) - (len(raw) % 2)])
    if not samples:
        return []

    buckets = max(1, buckets)
    size = max(1, len(samples) // buckets)
    out: list[float] = []
    for start in range(0, len(samples), size):
        window = samples[start:start + size]
        if not window:
            continue
        out.append(max(abs(min(window)), abs(max(window))) / 32768.0)
    return out[:buckets] or [0.0]


def tighten(
    path: Path | str,
    *,
    threshold_db: float = -40.0,
    min_silence: float = 0.25,
    ffmpeg: Path | str | None = None,
) -> Span | None:
    """The span of a clip with leading and trailing silence removed.

    Returns None when the whole clip is silence, so the caller can leave the
    padded cut alone rather than producing a zero-length file.
    """
    info = probe(path, ffmpeg=ffmpeg)
    if info.duration is None:
        return None
    silences = detect_silence(
        path, threshold_db=threshold_db, min_silence=min_silence, ffmpeg=ffmpeg
    )
    start, end = 0.0, info.duration
    for span in silences:
        if span.start <= 0.01:
            start = max(start, span.end)
        if span.end >= info.duration - 0.01:
            end = min(end, span.start)
    if end - start <= 0.01:
        return None
    return Span(start, end)
