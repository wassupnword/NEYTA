"""Runs inside uvr-local's interpreter, not NEYTA's.

Build plan 3.3 keeps the two Python environments separate on purpose: a 332 MB
Qt WebEngine wheel and a multi-hundred-MB torch stack have no business in one
resolver. This file is the subprocess boundary between them.

It must import nothing from `neyta` — that package does not exist on the other
side of the boundary. Standard library plus uvr only.

Protocol: one JSON object per line on stdout.

    {"event": "start",  "preset": "...", "models": 1}
    {"event": "step",   "index": 1, "of": 3, "model": "..."}
    {"event": "done",   "preset": "...", "stems": {"vocals": "/abs/path.wav"},
     "elapsed": 41.2}
    {"event": "error",  "message": "..."}

audio-separator's own chatter goes to stderr, so stdout stays parseable.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def emit(**payload) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="uvr-runner")
    parser.add_argument("--uvr-root", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--preset", required=True)
    parser.add_argument("--format", default="wav")
    parser.add_argument("--model-dir")
    args = parser.parse_args(argv)

    sys.path.insert(0, args.uvr_root)
    try:
        from uvr import PRESETS, UVR
    except Exception as exc:  # noqa: BLE001
        emit(event="error", message=f"cannot import uvr: {exc}")
        return 1

    if args.preset not in PRESETS:
        emit(event="error", message=f"unknown preset {args.preset!r}")
        return 2

    preset = PRESETS[args.preset]
    emit(event="start", preset=args.preset, models=len(preset.steps))

    kwargs = {
        "output_dir": args.output_dir,
        "output_format": args.format,
        "log_level": "WARNING",
        # Write into the directory we were given. NEYTA has already made it
        # unique, and it renames every stem afterwards anyway.
        "overwrite": True,
    }
    if args.model_dir:
        kwargs["model_dir"] = args.model_dir

    started = time.monotonic()
    try:
        uvr = UVR(**kwargs)
        stems = uvr.separate(
            args.input, preset=args.preset, out_dir=Path(args.output_dir)
        )
    except KeyboardInterrupt:
        emit(event="error", message="cancelled")
        return 130
    except Exception as exc:  # noqa: BLE001 — the parent renders the message
        emit(event="error", message=f"{type(exc).__name__}: {exc}")
        return 1

    emit(
        event="done",
        preset=args.preset,
        stems={name: str(path) for name, path in stems.items()},
        elapsed=round(time.monotonic() - started, 3),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
