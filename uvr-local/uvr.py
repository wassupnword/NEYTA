#!/usr/bin/env python3
"""
uvr.py — Ultimate Vocal Remover as a plain Python module + CLI.

Wraps `audio-separator` (the maintained pip port of the UVR engine, same
MDX-Net / BS-Roformer / VR-Arch / Demucs checkpoints the UVR GUI uses) so you
can call stem separation from other code without touching a GUI.

Setup (needs Python >= 3.10 and ffmpeg):
    brew install ffmpeg
    # Apple Silicon / CPU:
    pip install "audio-separator[cpu]"
    # NVIDIA:
    pip install "audio-separator[gpu]"

Library use:
    from uvr import separate, split_stems, PRESETS

    out = separate("song.wav", preset="vocals")
    print(out["vocals"], out["instrumental"])

    stems = split_stems("song.wav")           # drums/bass/other/vocals
    chain = separate("song.wav", preset="vocals_clean")  # vocals -> dereverb -> denoise

CLI use:
    python uvr.py song.wav
    python uvr.py song.wav --preset karaoke --format flac -o out/
    python uvr.py ./library --recursive --preset stems --jobs 1
    python uvr.py --list-presets
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

AUDIO_EXTS = {".wav", ".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".aiff", ".aif", ".wma"}

# Checkpoints live next to this script (a few hundred MB each) so the whole
# install stays self-contained. Override with UVR_MODEL_DIR.
DEFAULT_MODEL_DIR = Path(
    os.environ.get("UVR_MODEL_DIR") or Path(__file__).resolve().parent / "models"
)


# --------------------------------------------------------------------------
# Presets
# --------------------------------------------------------------------------
# A preset is an ordered chain of models. Each step runs on one stem produced
# by the previous step (`feed`), so you can e.g. isolate vocals then strip the
# reverb off just the vocal track.


@dataclass
class Step:
    model: str
    # Which stem from the previous step to feed into this one.
    # None = feed the original input file.
    feed: Optional[str] = None
    # Rename the stems this step produces, e.g. {"no reverb": "vocals"}.
    # Keys are matched case-insensitively against the stem label in the filename.
    rename: Dict[str, str] = field(default_factory=dict)


@dataclass
class Preset:
    description: str
    steps: List[Step]


PRESETS: Dict[str, Preset] = {
    "vocals": Preset(
        "Best-quality vocal / instrumental split (BS-Roformer).",
        [Step("model_bs_roformer_ep_317_sdr_12.9755.ckpt")],
    ),
    "vocals_fast": Preset(
        "Quicker vocal / instrumental split (MDX-Net Voc_FT). Good on CPU.",
        [Step("UVR-MDX-NET-Voc_FT.onnx")],
    ),
    "vocals_clean": Preset(
        "Vocals, then de-reverb, then de-noise the vocal stem.",
        [
            Step("model_bs_roformer_ep_317_sdr_12.9755.ckpt"),
            Step("UVR-DeEcho-DeReverb.pth", feed="vocals",
                 rename={"no reverb": "vocals", "reverb": "vocals_reverb"}),
            Step("UVR-DeNoise.pth", feed="vocals",
                 rename={"no noise": "vocals", "noise": "vocals_noise"}),
        ],
    ),
    "karaoke": Preset(
        "Instrumental + lead/backing vocal split (6_HP-Karaoke-UVR).",
        [
            Step("model_bs_roformer_ep_317_sdr_12.9755.ckpt"),
            Step("6_HP-Karaoke-UVR.pth", feed="vocals",
                 rename={"vocals": "lead_vocals", "instrumental": "backing_vocals"}),
        ],
    ),
    "stems": Preset(
        "4-stem split: drums / bass / other / vocals (htdemucs_ft).",
        [Step("htdemucs_ft.yaml")],
    ),
    "stems6": Preset(
        "6-stem split: drums / bass / guitar / piano / other / vocals (htdemucs_6s).",
        [Step("htdemucs_6s.yaml")],
    ),
    "dereverb": Preset(
        "Strip reverb/echo from a file, no vocal separation.",
        [Step("UVR-DeEcho-DeReverb.pth")],
    ),
    "denoise": Preset(
        "Strip noise/artifacts from a file, no vocal separation.",
        [Step("UVR-DeNoise.pth")],
    ),
}


class UVRError(RuntimeError):
    pass


def ensure_ffmpeg() -> str:
    """
    Guarantee an `ffmpeg` on PATH and return its path.

    audio-separator shells out to bare `ffmpeg`, which is missing when you run
    `.venv/bin/python uvr.py` directly (that doesn't put the venv's bin dir on
    PATH the way `activate` does). Look in three places, cheapest first:
    PATH, the bin dir next to this interpreter, then the static binary bundled
    in the imageio-ffmpeg wheel — symlinked into a stable spot so it's callable
    under the plain name `ffmpeg`.
    """
    import shutil

    found = shutil.which("ffmpeg")
    if found:
        return found

    def add(directory: Path) -> None:
        os.environ["PATH"] = f"{directory}{os.pathsep}{os.environ.get('PATH', '')}"

    venv_bin = Path(sys.executable).parent
    if (venv_bin / "ffmpeg").exists():
        add(venv_bin)
        return str(venv_bin / "ffmpeg")

    try:
        import imageio_ffmpeg

        exe = Path(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        raise UVRError(
            "ffmpeg not found. Run ./setup.sh in this folder, or install it "
            "yourself (brew install ffmpeg)."
        )

    # The wheel names it ffmpeg-macos-aarch64-v7.1; audio-separator calls
    # plain `ffmpeg`, so expose it under that name in a shim dir.
    shim = Path(__file__).resolve().parent / ".ffmpeg-shim"
    shim.mkdir(exist_ok=True)
    link = shim / "ffmpeg"
    if not link.exists():
        try:
            link.symlink_to(exe)
        except OSError:
            shutil.copy2(exe, link)
            link.chmod(0o755)
    add(shim)
    return str(link)


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------


class UVR:
    """Reusable separator. Keep one instance around to avoid reloading models."""

    def __init__(
        self,
        output_dir: str | os.PathLike = "separated",
        output_format: str = "wav",
        model_dir: str | os.PathLike = DEFAULT_MODEL_DIR,
        log_level: str = "WARNING",
        overwrite: bool = False,
        **separator_kwargs,
    ):
        self.output_dir = Path(output_dir)
        self.output_format = output_format
        self.model_dir = Path(model_dir)
        self.overwrite = overwrite
        self._log_level = log_level
        self._separator_kwargs = separator_kwargs
        self._sep = None
        self._loaded_model: Optional[str] = None

    # -- lazy init so importing this module stays cheap -------------------
    def _separator(self):
        if self._sep is None:
            try:
                import logging

                from audio_separator.separator import Separator
            except ImportError as e:  # pragma: no cover
                raise UVRError(
                    "audio-separator is not installed.\n"
                    '  pip install "audio-separator[cpu]"   # or [gpu] for CUDA\n'
                    "  (requires Python >= 3.10 and ffmpeg on PATH)"
                ) from e

            ensure_ffmpeg()
            self.output_dir.mkdir(parents=True, exist_ok=True)
            self.model_dir.mkdir(parents=True, exist_ok=True)
            self._sep = Separator(
                log_level=getattr(logging, self._log_level.upper(), logging.WARNING),
                model_file_dir=str(self.model_dir),
                output_dir=str(self.output_dir),
                output_format=self.output_format,
                **self._separator_kwargs,
            )
        return self._sep

    def load(self, model: str) -> None:
        sep = self._separator()
        if self._loaded_model != model:
            sep.load_model(model_filename=model)
            self._loaded_model = model

    def list_models(self) -> Dict[str, dict]:
        """Every model checkpoint audio-separator can download."""
        return self._separator().list_supported_model_files()

    def _claim_dir(self, base: Optional[Path] = None) -> Path:
        """
        Pick the folder this run writes into. Unless overwrite=True, never
        reuse a folder that already has files in it — fall back to
        'name-2', 'name-3', ... so previous runs are left untouched.
        """
        base = Path(base if base is not None else self.output_dir)
        target = base if self.overwrite else unique_dir(base)
        target.mkdir(parents=True, exist_ok=True)
        return target

    # -- one model, one file ----------------------------------------------
    def run_model(
        self,
        audio_path: str | os.PathLike,
        model: str,
        out_dir: Optional[str | os.PathLike] = None,
    ) -> Dict[str, Path]:
        audio_path = Path(audio_path)
        if not audio_path.is_file():
            raise UVRError(f"input file not found: {audio_path}")
        # out_dir is passed in by chained callers that already claimed a folder;
        # a direct call claims its own.
        dest = Path(out_dir) if out_dir is not None else self._claim_dir()
        dest.mkdir(parents=True, exist_ok=True)
        self.load(model)
        sep = self._separator()

        # The loaded model caches output_dir from load_model() time, so setting
        # it on the Separator alone is silently ignored — the stems land in the
        # old folder. Both have to be pointed at `dest`.
        sep.output_dir = str(dest)
        if getattr(sep, "model_instance", None) is not None:
            sep.model_instance.output_dir = str(dest)

        outputs = sep.separate(str(audio_path))

        stems: Dict[str, Path] = {}
        skipped: List[str] = []
        for out in outputs:
            path = Path(out)
            if not path.is_absolute():
                path = dest / path
            # audio-separator reports a filename for every stem the model can
            # emit, but silently skips writing one that came out near-silent
            # (common: asking a vocal model for vocals on an instrumental).
            # Report only what actually exists.
            if path.exists():
                stems[stem_label(path)] = path
            else:
                skipped.append(stem_label(path))

        if skipped:
            print(
                f"[uvr] {model}: no output for {', '.join(skipped)} "
                "(stem was silent) — skipping",
                file=sys.stderr,
            )
        if not stems:
            raise UVRError(
                f"{model} produced no output for {audio_path.name} "
                "(every stem came out silent)"
            )
        return stems

    # -- a preset chain, one file -----------------------------------------
    def separate(
        self,
        audio_path: str | os.PathLike,
        preset: str = "vocals",
        keep_intermediates: bool = False,
        out_dir: Optional[str | os.PathLike] = None,
    ) -> Dict[str, Path]:
        if preset not in PRESETS:
            raise UVRError(
                f"unknown preset {preset!r}. Known: {', '.join(sorted(PRESETS))}"
            )

        audio_path = Path(audio_path)
        # Claim one folder for the whole chain, so every step of this run lands
        # together and no earlier run is touched.
        dest = self._claim_dir(out_dir)
        stems: Dict[str, Path] = {}
        superseded: List[Path] = []

        for step in PRESETS[preset].steps:
            if step.feed is None:
                source = audio_path
            else:
                if step.feed not in stems:
                    raise UVRError(
                        f"preset {preset!r}: step {step.model} wants stem "
                        f"{step.feed!r}, which the previous step did not produce "
                        f"(got: {', '.join(stems)})"
                    )
                source = stems[step.feed]

            try:
                produced = self.run_model(source, step.model, out_dir=dest)
            except UVRError:
                # A first step that yields nothing is fatal, but a later
                # cleanup step (de-reverb, de-noise) yielding nothing just
                # means there was nothing to clean — keep the stems we have.
                if step.feed is None:
                    raise
                print(
                    f"[uvr] {step.model} produced nothing on the "
                    f"{step.feed} stem — leaving it as-is",
                    file=sys.stderr,
                )
                continue

            for label, path in produced.items():
                name = step.rename.get(label.lower(), label)
                if name in stems and stems[name] != path:
                    superseded.append(stems[name])
                stems[name] = path

        if not keep_intermediates:
            for path in superseded:
                if path not in stems.values():
                    path.unlink(missing_ok=True)

        return stems

    # -- a preset chain, many files ---------------------------------------
    def separate_batch(
        self,
        paths: Iterable[str | os.PathLike],
        preset: str = "vocals",
        per_track_subdir: bool = True,
        on_error: str = "warn",  # "warn" | "raise"
        keep_intermediates: bool = False,
    ) -> Dict[Path, Dict[str, Path]]:
        root = self.output_dir
        results: Dict[Path, Dict[str, Path]] = {}
        for raw in paths:
            path = Path(raw)
            base = root / safe_name(path.stem) if per_track_subdir else root
            try:
                results[path] = self.separate(
                    path,
                    preset=preset,
                    keep_intermediates=keep_intermediates,
                    out_dir=base,
                )
            except Exception as e:
                if on_error == "raise":
                    raise
                print(f"[uvr] FAILED {path.name}: {e}", file=sys.stderr)
                results[path] = {}
        return results


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def stem_label(filename: str | os.PathLike) -> str:
    """
    audio-separator names outputs '<track>_(<Stem>)_<model>.<ext>'.
    Pull '<Stem>' out and normalise it: 'No Reverb' -> 'no reverb'.

    Chained runs accumulate tags, so a de-reverbed vocal is named
    '<track>_(Vocals)_<roformer>_(No Reverb)_<deecho>.wav'. The stem this file
    actually *is* comes from the LAST tag — reading the first one labels both
    de-echo outputs 'vocals' and they collide.
    """
    import re

    tags = re.findall(r"_\(([^()]+)\)_", Path(filename).name)
    if tags:
        return tags[-1].strip().lower()
    return Path(filename).stem.lower()


def safe_name(name: str) -> str:
    """Make a track name usable as a folder name."""
    cleaned = "".join("_" if c in '/\\:*?"<>|' else c for c in name).strip(" .")
    return cleaned[:120] or "track"


def unique_dir(base: str | os.PathLike) -> Path:
    """
    Return a folder that is safe to write into: `base` itself if it doesn't
    exist or is empty, otherwise `base-2`, `base-3`, ... Nothing already on
    disk is ever touched.
    """
    def free(p: Path) -> bool:
        if not p.exists():
            return True
        return p.is_dir() and not any(p.iterdir())

    base = Path(base)
    if free(base):
        return base
    n = 2
    while True:
        candidate = base.with_name(f"{base.name}-{n}")
        if free(candidate):
            return candidate
        n += 1


def find_audio(target: str | os.PathLike, recursive: bool = False) -> List[Path]:
    """Expand a file or directory into a sorted list of audio files."""
    target = Path(target)
    if target.is_file():
        return [target]
    if not target.is_dir():
        raise UVRError(f"no such file or directory: {target}")
    it = target.rglob("*") if recursive else target.glob("*")
    return sorted(p for p in it if p.is_file() and p.suffix.lower() in AUDIO_EXTS)


# -- module-level convenience wrappers (one-shot use) ----------------------


def separate(
    audio_path: str | os.PathLike,
    preset: str = "vocals",
    output_dir: str | os.PathLike = "separated",
    output_format: str = "wav",
    overwrite: bool = False,
    **kwargs,
) -> Dict[str, Path]:
    """
    Separate one file with a preset. Returns {stem_name: Path}.

    Writes into `output_dir/<track name>/`; if that folder already has files
    in it, a fresh `<track name>-2/` is used instead, so no previous run is
    ever overwritten. Pass overwrite=True to write in place.
    """
    uvr = UVR(
        output_dir=output_dir,
        output_format=output_format,
        overwrite=overwrite,
        **kwargs,
    )
    track_dir = Path(output_dir) / safe_name(Path(audio_path).stem)
    return uvr.separate(audio_path, preset=preset, out_dir=track_dir)


def split_stems(
    audio_path: str | os.PathLike,
    output_dir: str | os.PathLike = "separated",
    six: bool = False,
    **kwargs,
) -> Dict[str, Path]:
    """Demucs multi-stem split. Returns {drums, bass, other, vocals, ...}."""
    return separate(
        audio_path, preset="stems6" if six else "stems", output_dir=output_dir, **kwargs
    )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="uvr",
        description="Ultimate Vocal Remover — stem separation from the command line.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join(
            ["presets:"] + [f"  {k:<14} {v.description}" for k, v in PRESETS.items()]
        ),
    )
    p.add_argument("input", nargs="*", help="audio file(s) or directory")
    p.add_argument("-p", "--preset", default="vocals", choices=sorted(PRESETS),
                   help="separation chain to run (default: vocals)")
    p.add_argument("-m", "--model", help="run this exact model instead of a preset")
    p.add_argument("-o", "--output-dir", default="separated", help="where stems go")
    p.add_argument("-f", "--format", default="wav", dest="output_format",
                   choices=["wav", "flac", "mp3", "m4a", "ogg", "opus"])
    p.add_argument("-r", "--recursive", action="store_true",
                   help="recurse into subdirectories when input is a directory")
    p.add_argument("--flat", action="store_true",
                   help="write stems straight into --output-dir, no per-track folder")
    p.add_argument("--overwrite", action="store_true",
                   help="write into the target folder even if it already has "
                        "files (default: never overwrite, use <name>-2 instead)")
    p.add_argument("--keep-intermediates", action="store_true",
                   help="keep stems that a later chain step replaced")
    p.add_argument("--model-dir", default=str(DEFAULT_MODEL_DIR),
                   help="where model checkpoints are cached/downloaded")
    p.add_argument("--list-presets", action="store_true")
    p.add_argument("--list-models", action="store_true",
                   help="list every downloadable model checkpoint")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="show audio-separator progress logs")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.list_presets:
        for name, preset in PRESETS.items():
            chain = " -> ".join(s.model for s in preset.steps)
            print(f"{name:<14} {preset.description}\n{'':<14} {chain}")
        return 0

    uvr = UVR(
        output_dir=args.output_dir,
        output_format=args.output_format,
        model_dir=args.model_dir,
        log_level="INFO" if args.verbose else "WARNING",
        overwrite=args.overwrite,
    )

    if args.list_models:
        import json

        print(json.dumps(uvr.list_models(), indent=2, default=str))
        return 0

    if not args.input:
        print("error: no input files given (use --list-presets to see options)",
              file=sys.stderr)
        return 2

    files: List[Path] = []
    for target in args.input:
        files.extend(find_audio(target, recursive=args.recursive))
    if not files:
        print("error: no audio files found", file=sys.stderr)
        return 1

    label = args.model or args.preset
    print(f"[uvr] {len(files)} file(s), {label} -> {args.output_dir}/", file=sys.stderr)

    failures = 0
    for i, path in enumerate(files, 1):
        print(f"[uvr] ({i}/{len(files)}) {path.name}", file=sys.stderr)
        root = Path(args.output_dir)
        base = root if args.flat else root / safe_name(path.stem)
        try:
            if args.model:
                dest = base if args.overwrite else unique_dir(base)
                stems = uvr.run_model(path, args.model, out_dir=dest)
            else:
                stems = uvr.separate(
                    path, preset=args.preset,
                    keep_intermediates=args.keep_intermediates,
                    out_dir=base,
                )
        except Exception as e:
            failures += 1
            print(f"[uvr] FAILED {path.name}: {e}", file=sys.stderr)
            continue
        for name, out in sorted(stems.items()):
            print(f"  {name:<16} {out}")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
