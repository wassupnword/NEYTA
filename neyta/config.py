"""Paths, defaults and the format matrix.

Nothing in here imports Qt. The format matrix is the single place that knows
what the three services can actually deliver, so that every part of the UI
showing a bitrate is showing the same measured number.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal

APP_NAME = "NEYTA"
APP_ORG = "neyta"
APP_ID = "com.neyta.app"

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Paths:
    """Every location the app writes to. Constructed once, injected everywhere.

    Tests build one of these pointing at a tmp_path, which is why no module
    below reaches for `Path.home()` on its own.
    """

    support: Path  # config, state
    cache: Path  # sqlite caches, temp media
    downloads: Path  # finished files, user-facing
    logs: Path

    @property
    def cache_db(self) -> Path:
        return self.cache / "cache.sqlite3"

    @property
    def slskd_dir(self) -> Path:
        return self.support / "slskd"

    @property
    def preview_dir(self) -> Path:
        """Soulseek fetch-then-play lands here; cleared on demand, not on exit."""
        return self.cache / "preview"

    @property
    def clips_dir(self) -> Path:
        """Phrase clips before trim/stem. Intermediate, safe to wipe."""
        return self.cache / "clips"

    def ensure(self) -> "Paths":
        for p in (
            self.support,
            self.cache,
            self.downloads,
            self.logs,
            self.preview_dir,
            self.clips_dir,
        ):
            p.mkdir(parents=True, exist_ok=True)
        return self

    @classmethod
    def default(cls) -> "Paths":
        home = Path.home()
        if sys.platform == "darwin":
            support = home / "Library" / "Application Support" / APP_NAME
            cache = home / "Library" / "Caches" / APP_ID
            logs = home / "Library" / "Logs" / APP_NAME
        else:  # only used by CI on Linux; the app itself is macOS-only
            xdg_data = Path(os.environ.get("XDG_DATA_HOME", home / ".local/share"))
            xdg_cache = Path(os.environ.get("XDG_CACHE_HOME", home / ".cache"))
            support = xdg_data / APP_NAME
            cache = xdg_cache / APP_NAME
            logs = support / "logs"
        return cls(
            support=support,
            cache=cache,
            downloads=home / "Music" / APP_NAME,
            logs=logs,
        )

    @classmethod
    def under(cls, root: Path) -> "Paths":
        """All four directories under one root. Used by tests and by the
        cold-start rehearsal in phase 8."""
        root = Path(root)
        return cls(
            support=root / "support",
            cache=root / "cache",
            downloads=root / "downloads",
            logs=root / "logs",
        )


# ---------------------------------------------------------------------------
# External binaries and interpreters
# ---------------------------------------------------------------------------

UVR_ROOT = REPO_ROOT / "uvr-local"
UVR_PYTHON = UVR_ROOT / ".venv" / "bin" / "python"
UVR_SCRIPT = UVR_ROOT / "uvr.py"
SOULSEEK_PKG_ROOT = REPO_ROOT / "soulseek"

#: lucida-flow (github.com/ryanlong1004/lucida-flow) drives lucida.to through
#: a headless browser and exposes a small local HTTP API in front of it. It
#: sits beside NEYTA rather than inside it, with its own interpreter — the same
#: arrangement as uvr-local, and for the same reason: playwright and chromium
#: are a dependency tree NEYTA has no business merging with its own.
LUCIDA_ROOT = REPO_ROOT / "lucida-flow"
LUCIDA_PYTHON = LUCIDA_ROOT / ".venv" / "bin" / "python"
LUCIDA_SERVER = LUCIDA_ROOT / "api_server.py"
#: Not lucida-flow's own default of 8000, which is the single most contested
#: port on a developer's machine.
LUCIDA_PORT = 8787
#: Which of lucida.to's services the tab is pointed at. The tab is called
#: Spotify because that is the streaming service people mean; lucida-flow
#: accepts the others by name too.
LUCIDA_SERVICE = "spotify"

#: samplette-local's crawled library. NEYTA reads it, read-only, and never
#: crawls into it — samplette-local stays the only writer, which is why the
#: Discogs token remains a samplette-local setting.
SAMPLETTE_ROOT = REPO_ROOT / "samplette-local"
SAMPLETTE_DB = SAMPLETTE_ROOT / "data" / "library.db"


def find_ffmpeg() -> Path | None:
    """System ffmpeg first, then the static arm64 binary in uvr-local's venv.

    Section 9 of the build plan: there is no Homebrew on this machine, so the
    imageio-ffmpeg binary is the only one that exists here. It is reached
    through uvr-local's venv rather than bundled twice.
    """
    system = shutil.which("ffmpeg")
    if system:
        return Path(system)
    vendored = UVR_ROOT / ".venv" / "bin" / "ffmpeg"
    if vendored.exists():
        # Deliberately NOT resolved. The real binary is named
        # `ffmpeg-macos-aarch64-v7.1`, and yt-dlp's `ffmpeg_location` wants a
        # path whose basename it recognises. Resolving the symlink makes
        # --download-sections fail with "ffmpeg is not installed".
        return vendored
    return None


def find_ffprobe() -> Path | None:
    system = shutil.which("ffprobe")
    if system:
        return Path(system)
    # imageio-ffmpeg ships ffmpeg only. ffmpeg alone can do everything the
    # convert layer needs, so a missing ffprobe is not fatal.
    return None


# ---------------------------------------------------------------------------
# Format matrix
# ---------------------------------------------------------------------------

ProviderKey = Literal[
    "youtube", "soundcloud", "bandcamp", "soulseek", "spotify"
]

#: Measured ceilings, not advertised ones. See build plan 2.1 — these came out
#: of actually probing the services, and they are why an "MP3 320" from the
#: first two is flagged as an upscale.
SOURCE_CEILING_KBPS: dict[str, int | None] = {
    # Nominal only. Format 140 (AAC) is constant at ~129.6k, but format 251
    # (opus) is variable — measured between 110k and 141k across a sample of
    # Topic-channel uploads, so it sometimes beats 140. This constant is a
    # display fallback; every upscale decision uses the bitrate actually
    # probed for the item in hand, which is why a 141k source is marked
    # correctly even though it exceeds the number here.
    "youtube": 129,
    "soundcloud": 160,  # hls_aac_160k
    "bandcamp": None,  # per-release: full lossless ladder, or a 128k preview
    "soulseek": None,  # peer-dependent: genuine FLAC and true 320 both occur
    # Service-dependent: FLAC from the lossless tiers, AAC/MP3 from the rest.
    # What arrives is whatever lucida.to could reach for that track.
    "spotify": None,
}

CEILING_NOTE: dict[str, str] = {
    "youtube": "YouTube's best audio is ~129k AAC, or Opus at a variable "
    "rate around it. Nothing here is close to 320.",
    "soundcloud": "SoundCloud's best audio stream is 160k AAC.",
    "bandcamp": "Bandcamp serves FLAC, WAV, AIFF and ALAC where the artist "
    "allows downloading, and a 128k MP3 preview where they do not.",
    "soulseek": "Soulseek serves whatever the peer has — often genuine "
    "FLAC or true 320.",
    "spotify": "Whatever lucida.to could reach for this track — FLAC from "
    "the lossless tiers, a lossy stream otherwise. The quality is a property "
    "of the copy it found, not of this tab.",
}


@dataclass(frozen=True)
class OutputFormat:
    key: str
    label: str
    ext: str
    #: pcm      decoded, no further loss (WAV)
    #: lossy    re-encoded to a target bitrate
    #: video    keeps the video stream
    #: source   whatever the peer/server already has, byte-for-byte
    kind: Literal["pcm", "lossy", "video", "source"]
    bitrate_kbps: int | None = None
    sample_rate: int | None = None
    bit_depth: int | None = None

    @property
    def is_audio_only(self) -> bool:
        return self.kind != "video"


WAV_48_24 = OutputFormat(
    key="wav_48_24",
    label="WAV 48kHz/24-bit",
    ext="wav",
    kind="pcm",
    sample_rate=48000,
    bit_depth=24,
)
WAV_44_16 = OutputFormat(
    key="wav_44_16",
    label="WAV 44.1kHz/16-bit",
    ext="wav",
    kind="pcm",
    sample_rate=44100,
    bit_depth=16,
)
MP3_320 = OutputFormat("mp3_320", "MP3 320kbps", "mp3", "lossy", bitrate_kbps=320)
MP3_192 = OutputFormat("mp3_192", "MP3 192kbps", "mp3", "lossy", bitrate_kbps=192)
MP3_128 = OutputFormat("mp3_128", "MP3 128kbps", "mp3", "lossy", bitrate_kbps=128)
M4A_SOURCE = OutputFormat("m4a_source", "M4A (source stream)", "m4a", "source")
MP4_VIDEO = OutputFormat("mp4_video", "MP4 (with video)", "mp4", "video")
ORIGINAL = OutputFormat("original", "Original file", "", "source")
#: Bandcamp hands over a real FLAC where the artist allows it. Keeping it as
#: its own option means the file lands lossless rather than being decoded to
#: WAV on the way in.
FLAC_SOURCE = OutputFormat("flac_source", "FLAC (lossless)", "flac", "source")

#: Per-tab menus. Order is display order.
FORMATS: dict[str, tuple[OutputFormat, ...]] = {
    "youtube": (WAV_48_24, WAV_44_16, MP3_320, MP3_192, MP3_128, M4A_SOURCE, MP4_VIDEO),
    "soundcloud": (WAV_48_24, WAV_44_16, MP3_320, MP3_192, MP3_128, M4A_SOURCE),
    "bandcamp": (ORIGINAL, FLAC_SOURCE, WAV_48_24, WAV_44_16,
                 MP3_320, MP3_192, MP3_128),
    "soulseek": (ORIGINAL, WAV_48_24, WAV_44_16, MP3_320, MP3_192, MP3_128),
    "spotify": (ORIGINAL, FLAC_SOURCE, WAV_48_24, WAV_44_16,
                MP3_320, MP3_192, MP3_128),
}

#: What a finished separation can be written as. UVR emits WAV; every other
#: entry here is one ffmpeg pass over that WAV, so only containers ffmpeg can
#: encode to appear — a "source" passthrough would be meaningless for a file
#: that did not exist until the model made it.
STEM_FORMATS: tuple[OutputFormat, ...] = (
    WAV_48_24, WAV_44_16, MP3_320, MP3_192, MP3_128,
)

DEFAULT_FORMAT: dict[str, str] = {
    "youtube": WAV_48_24.key,
    "soundcloud": WAV_48_24.key,
    # Every tab that can hand you the file the service itself has defaults to
    # keeping it rather than re-encoding it on the way in.
    "bandcamp": ORIGINAL.key,
    "soulseek": ORIGINAL.key,
    "spotify": ORIGINAL.key,
}


def formats_for(provider: str) -> tuple[OutputFormat, ...]:
    try:
        return FORMATS[provider]
    except KeyError:
        raise ValueError(f"unknown provider {provider!r}") from None


def format_by_key(key: str) -> OutputFormat:
    for group in FORMATS.values():
        for fmt in group:
            if fmt.key == key:
                return fmt
    raise ValueError(f"unknown format {key!r}")


def is_upscale(fmt: OutputFormat, source_kbps: float | None) -> bool:
    """True when the requested format claims more bitrate than the source has.

    A bigger file with no additional information. WAV is never an upscale:
    a decode of a 128k source is the honest full decode of that source, it is
    what UVR needs, and it is what Ableton wants. See build plan 2.1.
    """
    if fmt.kind != "lossy" or fmt.bitrate_kbps is None:
        return False
    if source_kbps is None:
        return False
    return fmt.bitrate_kbps > source_kbps


def upscale_note(fmt: OutputFormat, source_kbps: float | None) -> str | None:
    """The exact words shown next to a marked option, or None when honest."""
    if not is_upscale(fmt, source_kbps):
        return None
    assert fmt.bitrate_kbps is not None and source_kbps is not None
    ratio = fmt.bitrate_kbps / source_kbps
    return (
        f"upscale — source is {source_kbps:.0f}k, "
        f"this makes a {ratio:.1f}× larger file with no added detail"
    )


def annotate_formats(
    provider: str, source_kbps: float | None
) -> list[tuple[OutputFormat, str | None]]:
    """Every format for a tab paired with its upscale note (None when clean)."""
    return [(f, upscale_note(f, source_kbps)) for f in formats_for(provider)]


# ---------------------------------------------------------------------------
# Stems
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StemOption:
    key: str
    label: str
    #: Preset name in uvr-local/uvr.py, or None for the no-model passthrough.
    preset: str | None
    #: Which stem files of that preset's output this option is asking for.
    #: Empty means "all of them".
    stems: tuple[str, ...] = ()
    note: str = ""


#: Exposes all eight presets in uvr-local/uvr.py. Nothing is hidden.
STEM_OPTIONS: tuple[StemOption, ...] = (
    StemOption("original", "Original (no separation)", None, note="instant"),
    StemOption("vocals", "Vocals", "vocals", ("vocals",), "BS-Roformer"),
    StemOption(
        "instrumental",
        "Instrumental",
        "vocals",
        ("instrumental",),
        "free alongside vocals",
    ),
    StemOption("vocals_fast", "Vocals (fast)", "vocals_fast", (), "MDX-Net, quicker"),
    StemOption(
        "vocals_clean", "Vocals, de-reverbed + de-noised", "vocals_clean", ()
    ),
    StemOption("stems", "Drums / bass / other / vocals", "stems", (), "htdemucs_ft"),
    StemOption("stems6", "+ guitar / piano", "stems6", (), "htdemucs_6s"),
    StemOption("karaoke", "Lead vs backing vocals", "karaoke", ()),
    StemOption("dereverb", "De-reverbed", "dereverb", ()),
    StemOption("denoise", "De-noised", "denoise", ()),
)

DEFAULT_STEMS: tuple[str, ...] = ("original",)


def stem_option(key: str) -> StemOption:
    for opt in STEM_OPTIONS:
        if opt.key == key:
            return opt
    raise ValueError(f"unknown stem option {key!r}")


#: The stem options a cloud separator can actually produce. LALAL.AI splits one
#: stem at a time and hands back the leftover backing track with it, so vocals
#: and instrumental come out of one paid split. It has no "other" bucket and no
#: lead-versus-backing model, so the demucs options that promise those are not
#: offered when it is the chosen engine rather than being quietly reinterpreted
#: into something adjacent.
LALAL_STEM_OPTIONS: tuple[str, ...] = (
    "original", "vocals", "instrumental", "vocals_clean",
)


def presets_for(keys: Iterable[str]) -> list[str]:
    """The distinct UVR presets needed to satisfy a set of ticked options.

    'vocals' and 'instrumental' both come out of one BS-Roformer run, so
    ticking both must not run the model twice.
    """
    seen: list[str] = []
    for key in keys:
        preset = stem_option(key).preset
        if preset is not None and preset not in seen:
            seen.append(preset)
    return seen


# ---------------------------------------------------------------------------
# Engines
# ---------------------------------------------------------------------------
#
# Two jobs in this app can be done either by what is on this machine or by a
# paid service: finding a phrase, and pulling a stem out of a track. Both are
# declared the same way, so the settings page renders both from one widget and
# neither can offer an engine the rest of the app does not know how to build.
#
# The free engine is the default in both cases and stays fully functional. A
# paid one is an upgrade you switch on, never a wall you hit.


@dataclass(frozen=True)
class EngineOption:
    key: str
    label: str
    #: The key in settings.SERVICES whose credential this engine needs, or
    #: None when it needs none. This is what lets the settings page say "needs
    #: a key" without a second list of which engines are paid.
    service: str | None
    note: str
    #: True when running it spends money or a metered allowance.
    paid: bool = False

    @property
    def free(self) -> bool:
        return not self.paid


PHRASE_ENGINES: tuple[EngineOption, ...] = (
    EngineOption(
        key="builtin",
        label="NEYTA — top results, read for real",
        service=None,
        note=(
            "Searches YouTube for the phrase and reads the captions of the "
            "top results. Broad but not exhaustive, and it says so."
        ),
    ),
    EngineOption(
        key="filmot",
        label="Filmot — caption index",
        service="filmot",
        note=(
            "A real index of what has been said on YouTube, so the phrase is "
            "looked up rather than guessed at. Needs a supporter API key, and "
            "returns the video and the timestamp directly."
        ),
        paid=True,
    ),
)

STEM_ENGINES: tuple[EngineOption, ...] = (
    EngineOption(
        key="uvr",
        label="UVR — this machine",
        service=None,
        note=(
            "Runs the models locally. Free and unmetered, as fast as this "
            "machine is, and nothing leaves the computer."
        ),
    ),
    EngineOption(
        key="lalal",
        label="LALAL.AI — cloud",
        service="lalal",
        note=(
            "Uploads the track and separates it on their machines. Costs "
            "minutes from your plan and needs a licence key, but it does not "
            "tie up this computer and it splits vocals well."
        ),
        paid=True,
    ),
)

DEFAULT_PHRASE_ENGINE = "builtin"
DEFAULT_STEM_ENGINE = "uvr"


def engine(options: tuple[EngineOption, ...], key: str) -> EngineOption:
    for option in options:
        if option.key == key:
            return option
    raise ValueError(f"unknown engine {key!r}")


def phrase_engine(key: str) -> EngineOption:
    return engine(PHRASE_ENGINES, key)


def stem_engine(key: str) -> EngineOption:
    return engine(STEM_ENGINES, key)


def stem_options_for(engine: str) -> tuple[str, ...]:
    """Which stem options an engine can actually deliver.

    The picker greys out the rest with the reason, rather than offering a
    choice that would come back as something else.
    """
    if engine == "lalal":
        return LALAL_STEM_OPTIONS
    return tuple(option.key for option in STEM_OPTIONS)


# ---------------------------------------------------------------------------
# Network / rate limiting
# ---------------------------------------------------------------------------

#: HTTP 429 is real and was hit during caption fetching (build plan 0).
CAPTION_WORKERS = 4
CAPTION_BACKOFF_BASE = 1.5
CAPTION_BACKOFF_MAX = 60.0
CAPTION_MAX_RETRIES = 5

PHRASE_CANDIDATES = 30
SEARCH_TTL_SECONDS = 60 * 60 * 6
PROBE_TTL_SECONDS = 60 * 60 * 2
#: Captions never expire: the transcript of a published video does not change.
CAPTION_TTL_SECONDS = None

SOULSEEK_SEARCH_TIMEOUT = 30.0
