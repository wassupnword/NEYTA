#!/usr/bin/env python
"""Regenerate tests/fixtures/*.json from the live services.

The unit suite runs entirely against these files, so it needs no network. Run
this when a service changes shape and the offline tests start disagreeing with
reality — that disagreement is the signal, and silently loosening a test to
match would throw it away.

    .venv-neyta/bin/python tools/refresh_fixtures.py

Only the fields NEYTA reads are kept, so the fixtures stay small enough to
review in a diff.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from neyta.core.engine import Engine, EngineError  # noqa: E402

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

#: "Me at the zoo" — short, permanent, and its ladder contains everything the
#: tests care about: the 129k AAC ceiling, opus, the muxed 360p format 18 that
#: must never be chosen for audio, and storyboards that must be filtered out.
YOUTUBE_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
YOUTUBE_QUERY = "me at the zoo"

#: A SoundCloud track that credits an artist separately from the uploading
#: account, and is not DRM-protected. Label uploads frequently are.
SOUNDCLOUD_QUERY = "aphex twin xtal slowed"

ENTRY_FIELDS = (
    "id", "title", "track", "album", "duration", "uploader", "channel",
    "artist", "artists",
    "url", "webpage_url", "thumbnails", "view_count",
)
FORMAT_FIELDS = (
    "format_id", "ext", "abr", "tbr", "acodec", "vcodec", "asr",
    "filesize", "filesize_approx", "format_note", "protocol",
)


def trim_entry(entry: dict) -> dict:
    out = {k: entry[k] for k in ENTRY_FIELDS if entry.get(k) is not None}
    if "thumbnails" in out:
        out["thumbnails"] = [{"url": t.get("url")} for t in out["thumbnails"][-1:]]
    return out


def trim_info(info: dict) -> dict:
    out = trim_entry(info)
    out["formats"] = [
        {k: f[k] for k in FORMAT_FIELDS if f.get(k) is not None}
        for f in info.get("formats", [])
    ]
    return out


def write(name: str, payload) -> None:
    path = FIXTURES / name
    path.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", "utf-8")
    print(f"  {name:<28} {path.stat().st_size / 1024:.1f} KB")


def build_tone() -> None:
    """0.4s silence, 1.0s of 440Hz, 0.4s silence — 44.1kHz mono 16-bit.

    Deterministic and tiny. Exercises sample-rate conversion (44.1k is not the
    48k default output), mono preservation, span cutting, and silence
    detection at both ends, all from one 155 KB file.
    """
    import subprocess

    from neyta import config as cfg

    ffmpeg = cfg.find_ffmpeg()
    if ffmpeg is None:
        print("  no ffmpeg — tone.wav left unchanged")
        return
    dest = FIXTURES / "tone.wav"
    subprocess.run(
        [str(ffmpeg), "-hide_banner", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=0.4",
         "-f", "lavfi", "-i", "sine=frequency=440:r=44100:d=1.0",
         "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=0.4",
         "-filter_complex", "[0][1][2]concat=n=3:v=0:a=1[a]", "-map", "[a]",
         "-c:a", "pcm_s16le", str(dest)],
        check=True, capture_output=True,
    )
    print(f"  {'tone.wav':<28} {dest.stat().st_size / 1024:.1f} KB")


def main() -> int:
    FIXTURES.mkdir(parents=True, exist_ok=True)

    print("Local audio")
    build_tone()

    engine = Engine()  # no cache: fixtures must come from the service

    print("YouTube")
    write("youtube_search.json",
          [trim_entry(e) for e in engine.search("ytsearch", YOUTUBE_QUERY, 3)])
    yt = trim_info(engine.extract(YOUTUBE_URL))
    write("youtube_extract.json", yt)
    audio = [(f["format_id"], f.get("abr")) for f in yt["formats"]
             if f.get("vcodec") == "none"]
    print(f"  audio ladder: {audio}")

    print("SoundCloud")
    entries = engine.search("scsearch", SOUNDCLOUD_QUERY, 5)
    chosen = None
    for entry in entries:
        if not entry.get("artists") and not entry.get("artist"):
            continue
        url = entry.get("webpage_url") or entry.get("url")
        try:
            info = engine.extract(url)
        except EngineError as exc:
            print(f"  skipping {entry.get('title')!r}: {exc}")
            continue
        chosen = (entry, info)
        break

    if chosen is None:
        print("  no usable SoundCloud track found — fixtures left unchanged")
        return 1

    entry, info = chosen
    write("soundcloud_search.json", [trim_entry(entry)])
    sc = trim_info(info)
    write("soundcloud_extract.json", sc)
    print(f"  artist={entry.get('artists') or entry.get('artist')} "
          f"uploader={entry.get('uploader')}")
    print(f"  ladder: {[(f['format_id'], f.get('abr')) for f in sc['formats']]}")

    print("Bandcamp")
    build_bandcamp(engine)

    print("Captions")
    build_captions(engine)
    return 0


#: A TED talk: automatic captions with per-word tOffsetMs.
CAPTIONS_AUTO = "https://www.youtube.com/watch?v=8jPQjjsBbIc"
#: "Me at the zoo": human-uploaded captions, one seg per line, no offsets.
CAPTIONS_MANUAL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"


def build_captions(engine) -> None:
    """Capture one track of each quality, trimmed to a reviewable size.

    The whole phrase matcher turns on the difference between them, so both
    fixtures are kept even though they look nearly identical in structure.
    """
    import requests

    from neyta.core import captions as caps

    for name, url, want in (
        ("captions_auto.json", CAPTIONS_AUTO, "auto"),
        ("captions_manual.json", CAPTIONS_MANUAL, "manual"),
    ):
        try:
            info = engine.extract(url)
        except EngineError as exc:
            print(f"  {name}: {exc}")
            continue
        source = (info.get("automatic_captions") if want == "auto"
                  else info.get("subtitles")) or {}
        track = next(
            (t for lang, tracks in source.items() if lang.startswith("en")
             for t in tracks if t.get("ext") == "json3"), None
        )
        if track is None:
            print(f"  {name}: no json3 track")
            continue
        # This endpoint 429s readily, which is the whole reason the runtime
        # fetcher has backoff. Do the same here rather than writing a
        # half-captured fixture.
        payload = None
        for attempt in range(6):
            response = requests.get(track["url"], timeout=30)
            if response.status_code == 200:
                try:
                    payload = response.json()
                    break
                except ValueError:
                    pass
            print(f"    HTTP {response.status_code}, retrying "
                  f"({attempt + 1}/6)")
            time.sleep(2 ** attempt)
        if payload is None:
            print(f"  {name}: could not fetch captions")
            continue
        # Enough events to match multi-word phrases across line breaks.
        payload["events"] = (payload.get("events") or [])[:40]
        write(name, payload)
        kind = caps.detect_kind(payload)
        offsets = sum(1 for ev in payload["events"]
                      for s in (ev.get("segs") or []) if "tOffsetMs" in s)
        print(f"    kind={kind}  word offsets={offsets}")


#: A release the artist made downloadable — the full lossless ladder.
BANDCAMP_DOWNLOADABLE = "https://oylumtanis.bandcamp.com/track/burial-archangel"
#: A paid release — the same extractor returns only the 128k stream preview.
#: Having both is the point: the ladder is a property of the release, not of
#: the service, and the upscale marking has to follow it.
BANDCAMP_PREVIEW_ONLY = "https://burial.bandcamp.com/track/archangel"
BANDCAMP_QUERY = "burial archangel"


def build_bandcamp(engine) -> None:
    import requests

    from neyta.providers.bandcamp import SEARCH_URL, TRACK_FILTER, _SEARCH_DEFAULTS

    response = requests.post(
        SEARCH_URL,
        json={"search_text": BANDCAMP_QUERY, "search_filter": TRACK_FILTER,
              **_SEARCH_DEFAULTS},
        headers={"User-Agent": "Mozilla/5.0"}, timeout=20,
    )
    hits = [h for h in (response.json().get("auto") or {}).get("results", [])
            if h.get("type") == TRACK_FILTER][:5]
    write("bandcamp_search.json", hits)

    for name, url in (("bandcamp_extract_downloadable.json", BANDCAMP_DOWNLOADABLE),
                      ("bandcamp_extract_preview.json", BANDCAMP_PREVIEW_ONLY)):
        try:
            info = trim_info(engine.extract(url))
        except EngineError as exc:
            print(f"  {name}: {exc}")
            continue
        write(name, info)
        print(f"    ladder: {[f['format_id'] for f in info['formats']]}")


if __name__ == "__main__":
    sys.exit(main())
