"""A stand-in for neyta/core/uvr_runner.py that speaks the same protocol.

Lets the driver — subprocess handling, JSON parsing, progress, cancellation,
calibration — be tested without loading torch or waiting minutes for a real
separation. Behaviour is steered by environment variables:

    FAKE_UVR_MODE=ok|error|silent|crash|slow|garbage
    FAKE_UVR_SLEEP=<seconds>
    FAKE_UVR_STEMS=vocals,instrumental
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def emit(**payload) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uvr-root")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--preset", required=True)
    parser.add_argument("--format", default="wav")
    parser.add_argument("--model-dir")
    args = parser.parse_args()

    mode = os.environ.get("FAKE_UVR_MODE", "ok")
    sleep = float(os.environ.get("FAKE_UVR_SLEEP", "0"))
    names = os.environ.get("FAKE_UVR_STEMS", "vocals,instrumental").split(",")

    if mode == "crash":
        sys.stderr.write("boom\n")
        return 3
    if mode == "garbage":
        sys.stdout.write("not json at all\nstill not json\n")
        sys.stdout.flush()
        return 0
    if mode == "silent":
        return 0  # exits cleanly having said nothing

    emit(event="start", preset=args.preset, models=1)

    if mode == "slow" or sleep:
        deadline = time.monotonic() + (sleep or 30)
        while time.monotonic() < deadline:
            time.sleep(0.05)

    if mode == "error":
        emit(event="error", message="RuntimeError: model failed to load")
        return 1

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    stems = {}
    for name in names:
        name = name.strip()
        if not name:
            continue
        path = out / f"input_({name.title()})_fake.{args.format}"
        path.write_bytes(b"RIFF" + b"\0" * 256)
        stems[name] = str(path)

    emit(event="done", preset=args.preset, stems=stems, elapsed=0.25)
    return 0


if __name__ == "__main__":
    sys.exit(main())
