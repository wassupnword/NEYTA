"""`python -m neyta search|formats|get` — the engine without the UI.

The runnable end of phase 2: search both services, see true bitrates and
upscale marks, write a correct WAV.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import config
from .core import convert, naming
from .core.cache import Cache
from .core.engine import Engine, EngineError, Unavailable
from .providers.base import Provider, Result
from .providers.bandcamp import BandcampProvider
from .providers.soundcloud import SoundCloudProvider
from .providers.youtube import YouTubeProvider

PROVIDERS: dict[str, type[Provider]] = {
    "youtube": YouTubeProvider,
    "soundcloud": SoundCloudProvider,
    "bandcamp": BandcampProvider,
}


def build_provider(name: str, engine: Engine) -> Provider:
    return PROVIDERS[name](engine)


def _duration(seconds: float | None) -> str:
    if not seconds:
        return "   —  "
    m, s = divmod(int(seconds), 60)
    return f"{m:>3}:{s:02d}"


def _bar(fraction: float, width: int = 28) -> str:
    filled = int(fraction * width)
    return "█" * filled + "·" * (width - filled)


class Reporter:
    """One rewriting progress line, quiet when not a terminal."""

    def __init__(self, label: str, stream=sys.stderr) -> None:
        self.label = label
        self.stream = stream
        self.tty = stream.isatty()
        self._last = -1.0

    def __call__(self, fraction: float, message: str = "") -> None:
        if not self.tty or fraction - self._last < 0.01:
            return
        self._last = fraction
        print(
            f"\r  {self.label}  {_bar(fraction)} {fraction * 100:5.1f}%  {message:<14}",
            end="", file=self.stream, flush=True,
        )

    def done(self) -> None:
        if self.tty:
            print("\r" + " " * 78 + "\r", end="", file=self.stream, flush=True)


# ---------------------------------------------------------------------------


def cmd_search(args, engine: Engine) -> int:
    names = list(PROVIDERS) if args.on == "both" else [args.on]
    found = 0

    for name in names:
        provider = build_provider(name, engine)
        print(f"\n\033[1m{provider.label}\033[0m — {provider.ceiling_note}")
        try:
            results = provider.search(args.query, args.limit)
        except EngineError as exc:
            print(f"  search failed: {exc}", file=sys.stderr)
            continue

        if not results:
            print("  no results")
            continue

        for i, r in enumerate(results, 1):
            found += 1
            artist = f"{r.artist} — " if r.artist else ""
            print(
                f"  {i:>2}. {_duration(r.duration)}  {r.display_bitrate:>5}  "
                f"{artist}{r.title}"
            )
            if args.urls:
                print(f"      {r.url}")

    if found:
        print(f"\n{found} results. `neyta formats <url>` for the stream ladder.")
    return 0 if found else 1


def cmd_formats(args, engine: Engine) -> int:
    provider = build_provider(args.on, engine)
    result = Result(provider=provider.key, id="", title=args.url, url=args.url)

    try:
        media = provider.probe(result)
    except EngineError as exc:
        print(f"probe failed: {exc}", file=sys.stderr)
        return 1

    r = media.result
    print(f"\n\033[1m{r.artist or '?'} — {r.title}\033[0m  ({_duration(r.duration)})")

    print("\n  streams the service actually has:")
    for s in sorted(media.streams, key=lambda s: (s.has_video, s.bitrate_kbps or 0)):
        rate = f"{s.bitrate_kbps:.0f}k" if s.bitrate_kbps else "—"
        kind = "video" if s.has_video else "audio"
        asr = f" {s.sample_rate}Hz" if s.sample_rate else ""
        print(f"    {s.id:>16} {s.ext:>5} {rate:>7} {kind}{asr}  {s.codec or ''}")

    source = media.source_kbps
    best = media.best_audio
    print(f"\n  best audio: {media.quality_label}"
          + (f" ({best.id})" if best else ""))
    if media.lossless:
        print("  \033[32mlossless — the artist enabled downloading, so this is "
              "the file they uploaded\033[0m")
        print("  nothing below is marked as an upscale: there is no bitrate "
              "ceiling to inflate past.")

    print("\n  output formats:")
    for opt in provider.format_options(media):
        if not opt.available:
            print(f"   \033[2m× {opt.format.key:<12} {opt.format.label} "
                  f"— {opt.note}\033[0m")
            continue
        mark = "\033[33m!\033[0m" if opt.note else " "
        print(f"   {mark} {opt.format.key:<12} {opt.format.label}")
        if opt.note:
            print(f"       \033[33m{opt.note}\033[0m")
    return 0


def cmd_get(args, engine: Engine) -> int:
    provider = build_provider(args.on, engine)
    fmt = config.format_by_key(args.format)
    if fmt not in provider.formats():
        print(
            f"{fmt.key} is not offered on the {provider.label} tab. "
            f"Options: {', '.join(f.key for f in provider.formats())}",
            file=sys.stderr,
        )
        return 2

    wants_span = args.end is not None or args.start
    if wants_span and not getattr(provider, "supports_spans", False):
        print(
            f"--start/--end are YouTube-only. The {provider.label} tab "
            "downloads whole tracks.",
            file=sys.stderr,
        )
        return 2

    stub = Result(provider=provider.key, id="", title=args.url, url=args.url)
    try:
        media = provider.probe(stub)
    except EngineError as exc:
        print(f"probe failed: {exc}", file=sys.stderr)
        return 1

    r = media.result
    source = media.source_kbps
    print(f"{r.artist or '?'} — {r.title}")
    print(f"  source   {media.quality_label}"
          + ("  (lossless)" if media.lossless else ""))

    if note := config.upscale_note(fmt, source):
        print(f"  \033[33mwarning  {note}\033[0m")

    out_dir = Path(args.out or config.Paths.default().downloads)
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = naming.resolve_output(
        out_dir, title=r.title, artist=r.artist, ext=fmt.ext or "audio"
    )

    reporter = Reporter(fmt.key)
    span = (args.start, args.end) if args.end is not None else None
    try:
        written = provider.fetch(stub, fmt, dest, progress=reporter, span=span)
    except (EngineError, convert.ConversionError) as exc:
        reporter.done()
        print(f"fetch failed: {exc}", file=sys.stderr)
        return 1
    reporter.done()

    print(f"  wrote    {written}")
    try:
        info = convert.probe(written)
    except convert.ConversionError:
        return 0

    bits = []
    if info.sample_rate:
        bits.append(f"{info.sample_rate}Hz")
    if info.channels:
        bits.append({1: "mono", 2: "stereo"}.get(info.channels, f"{info.channels}ch"))
    if info.duration:
        bits.append(f"{info.duration:.2f}s")
    if info.codec:
        bits.append(info.codec)
    print(f"  verified {' · '.join(bits)}  ({written.stat().st_size / 1e6:.1f} MB)")
    return 0


# ---------------------------------------------------------------------------


def parse_range(text: str | None) -> "samplette.Range":
    """`90-150`, `90-`, `-150`, or `120` (exact)."""
    from .core import samplette

    if not text:
        return samplette.Range()
    text = text.strip()
    if "-" not in text:
        value = float(text)
        return samplette.Range(value, value)
    lo, _, hi = text.partition("-")
    return samplette.Range(
        float(lo) if lo.strip() else None, float(hi) if hi.strip() else None
    )


def cmd_shuffle(args, engine: Engine) -> int:
    from .core import samplette

    try:
        library = samplette.SampletteLibrary()
    except samplette.SampletteUnavailable as exc:
        print(exc, file=sys.stderr)
        return 1

    with library:
        if args.stats:
            s = library.stats()
            print(f"  total      {s.total:>8,}")
            print(f"  playable   {s.ready:>8,}  ({s.ready_fraction:.1%})")
            print(f"  queued     {s.pending:>8,}  samplette-local resolves these")
            print(f"  with BPM   {s.with_key_and_tempo:>8,}")
            return 0

        if args.facets:
            for name in ("genres", "styles", "regions", "keys"):
                top = library.facet(name, 12)
                print(f"\n\033[1m{name}\033[0m")
                for value, n in top:
                    print(f"  {n:>6}  {value}")
            for name in ("tempo", "year", "views"):
                lo, hi = library.bounds(name)
                print(f"\n{name}: {lo} .. {hi}")
            return 0

        filters = samplette.Filters(
            query=args.query or "",
            genres=samplette.TagFilter.of(*(args.genre or [])),
            styles=samplette.TagFilter.of(*(args.style or [])),
            tags=samplette.TagFilter.of(*(args.tag or [])),
            regions=samplette.TagFilter.of(*(args.region or [])),
            keys=samplette.TagFilter.of(*(args.key or [])),
            tempo=parse_range(args.tempo),
            year=parse_range(args.year),
            duration=parse_range(args.duration),
            topic_only=args.topic_only,
        )

        matching = library.count(filters)
        if not matching:
            print("nothing matches those filters. `neyta shuffle --facets` "
                  "shows what the library actually has.", file=sys.stderr)
            return 1

        print(f"\n{matching:,} playable tracks match\n")

        def show(track) -> None:
            print(f"  \033[1m{track.summary}\033[0m")
            extras = [e for e in (track.release, track.label) if e]
            if track.yt_views:
                extras.append(f"{track.yt_views:,} views")
            if extras:
                print(f"    {' · '.join(extras)}")
            print(f"    {track.url}")

        if not args.get:
            for track in library.sample(args.number, filters, mode=args.mode):
                show(track)
            return 0

        # A shuffled track is an ordinary YouTube result: same fetch path,
        # same format matrix, same naming.
        provider = build_provider("youtube", engine)
        fmt = config.format_by_key(args.format)
        out_dir = Path(args.out or config.Paths.default().downloads)
        out_dir.mkdir(parents=True, exist_ok=True)

        # Roughly half of YouTube's rights-restricted music answers the media
        # URL with a 403 no matter which player client asks. When you are
        # digging you want *a* record, not that one specifically, so a dead
        # result means try the next — the same rule the plan sets for a
        # Soulseek peer vanishing mid-transfer. One draw, printed as it is
        # attempted, so what is listed is always what was fetched.
        pool = library.sample(max(args.number * 8, 24), filters, mode=args.mode)
        wrote = skipped = 0

        for track in pool:
            if wrote >= args.number:
                break
            show(track)
            result = track.to_result()
            dest = naming.resolve_output(
                out_dir, title=result.title, artist=result.artist,
                ext=fmt.ext or "audio",
            )
            reporter = Reporter(fmt.key)
            try:
                written = provider.fetch(result, fmt, dest, progress=reporter)
            except Unavailable:
                reporter.done()
                skipped += 1
                print("    \033[2mskipped — YouTube refuses this stream\033[0m")
                continue
            except (EngineError, convert.ConversionError) as exc:
                reporter.done()
                skipped += 1
                print(f"    skip: {exc}", file=sys.stderr)
                continue
            reporter.done()
            wrote += 1
            print(f"    \033[1mwrote\033[0m {written}")

        if wrote < args.number:
            print(
                f"\n  got {wrote} of {args.number} after {wrote + skipped} tries. "
                "YouTube refuses the audio stream on a lot of rights-held "
                "music; widen the filters or try again.",
                file=sys.stderr,
            )
            return 1
        if skipped:
            print(f"\n  {wrote} downloaded, {skipped} skipped as restricted.")
    return 0


def cmd_stems(args, engine: Engine) -> int:
    from .core import convert, stems as stems_core

    paths = config.Paths.default().ensure()
    calibration = stems_core.Calibration(path=paths.support / "calibration.json")
    separator = stems_core.StemSeparator(calibration=calibration)

    if args.list:
        print(f"\n  {'option':<16} {'preset':<14} rate on this machine")
        for option in config.STEM_OPTIONS:
            if option.preset is None:
                print(f"  {option.key:<16} {'—':<14} instant")
                continue
            rate = calibration.rate(option.preset)
            measured = f"{rate:.2f}x realtime" if rate else "not measured yet"
            missing = stems_core.missing_models(option.preset)
            note = f"  (downloads {len(missing)} model)" if missing else ""
            print(f"  {option.key:<16} {option.preset:<14} {measured}{note}")
        return 0

    if not separator.available():
        print("uvr-local is not built. Run tools/setup.sh.", file=sys.stderr)
        return 1

    audio = Path(args.input)
    if not audio.exists():
        print(f"no such file: {audio}", file=sys.stderr)
        return 1

    keys = [k.strip() for k in args.pick.split(",") if k.strip()]
    try:
        steps = stems_core.plan_separation(keys)
    except ValueError as exc:
        print(f"{exc}\nTry: neyta stems --list", file=sys.stderr)
        return 2
    if not steps:
        print("nothing to separate — pick at least one stem", file=sys.stderr)
        return 2

    try:
        duration = convert.probe(audio).duration
    except convert.ConversionError:
        duration = None

    print(f"{audio.name}")
    print(f"  {calibration.describe(steps, duration or 0.0)}")
    print(f"  running: {', '.join(s.preset for s in steps)}")

    out_dir = Path(args.out or config.Paths.default().downloads)
    scratch = paths.cache / "stems" / audio.stem
    reporter = Reporter("stems")
    try:
        raw = separator.separate(
            audio, keys, scratch, audio_seconds=duration,
            progress=reporter,
        )
    except stems_core.StemError as exc:
        reporter.done()
        print(f"separation failed: {exc}", file=sys.stderr)
        return 1
    reporter.done()

    delivered = stems_core.deliver(
        raw, out_dir, title=audio.stem, artist=args.artist
    )
    for name, path in sorted(delivered.items()):
        print(f"  {name:<16} {path}")
    if duration:
        print(f"\n  calibration: "
              + ", ".join(f"{k} {v:.2f}x" for k, v in sorted(calibration.rates.items())))
    return 0 if delivered else 1


def cmd_phrase(args, engine: Engine) -> int:
    from .core import phrase as P

    reporter = Reporter("phrase")
    search = P.discover(
        args.phrase, engine, candidates=args.candidates,
        fuzzy=not args.exact, progress=reporter,
    )
    reporter.done()

    print(f"\n\033[2m{search.summary}\033[0m")
    if not search.hits:
        print("\nNothing found. Phrase search reads the captions of the top "
              "results — it is not an index of all of YouTube.", file=sys.stderr)
        return 1

    for i, hit in enumerate(search.hits[:args.limit], 1):
        colour = "\033[32m" if hit.accuracy == "word" else "\033[33m"
        score = "" if hit.score >= 0.999 else f"  ~{hit.score:.2f}"
        print(f"\n  {i:>2}. {colour}[{hit.badge}]\033[0m {hit.label}{score}")
        print(f"      {hit.title[:66]}")
        print(f"      \033[1m{hit.matched}\033[0m")
        print(f"      \033[2m…{hit.context[:96]}…\033[0m")
        if not args.get:
            print(f"      {hit.url}")

    if not args.get:
        print("\n  --get N downloads hit N as a trimmed clip.")
        return 0

    index = args.get - 1
    if not 0 <= index < len(search.hits):
        print(f"no hit {args.get}", file=sys.stderr)
        return 2
    hit = search.hits[index]

    provider = build_provider("youtube", engine)
    fmt = config.format_by_key(args.format)
    out_dir = Path(args.out or config.Paths.default().downloads)
    out_dir.mkdir(parents=True, exist_ok=True)

    lo, hi = hit.padded()
    print(f"\n  cutting {lo:.2f}s → {hi:.2f}s")
    dest = naming.resolve_output(
        out_dir, title=hit.matched, artist=hit.uploader, ext=fmt.ext or "wav"
    )
    grab = Reporter("clip")
    try:
        written = provider.fetch(
            Result(provider="youtube", id=hit.video_id, title=hit.matched,
                   url=hit.url),
            fmt, dest, progress=grab, span=(lo, hi),
        )
    except (EngineError, convert.ConversionError) as exc:
        grab.done()
        print(f"  fetch failed: {exc}", file=sys.stderr)
        return 1
    grab.done()
    print(f"  wrote {written}")

    if args.no_trim:
        return 0

    span = convert.tighten(written, min_silence=0.15, threshold_db=-38)
    if span is None:
        print("  auto-trim: the clip is silence — left as cut")
        return 0
    if span.start < 0.02 and abs(span.end - (hi - lo)) < 0.05:
        print("  auto-trim: already tight")
        return 0
    trimmed = written.with_name(written.stem + "-trimmed" + written.suffix)
    convert.transcode(written, trimmed, fmt, start=span.start, end=span.end)
    print(f"  trimmed {span.duration:.2f}s → {trimmed}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="neyta", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("search", help="search YouTube and/or SoundCloud")
    s.add_argument("query")
    s.add_argument("--on", choices=[*PROVIDERS, "both"], default="both")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--urls", action="store_true", help="print each result's URL")
    s.set_defaults(func=cmd_search)

    f = sub.add_parser("formats", help="show a track's real stream ladder")
    f.add_argument("url")
    f.add_argument("--on", choices=list(PROVIDERS), default="youtube")
    f.set_defaults(func=cmd_formats)

    g = sub.add_parser("get", help="download and convert")
    g.add_argument("url")
    g.add_argument("--on", choices=list(PROVIDERS), default="youtube")
    g.add_argument("--format", default=config.WAV_48_24.key)
    g.add_argument("--out", help="output directory")
    g.add_argument("--start", type=float, default=0.0,
                   help="cut from, seconds (YouTube only)")
    g.add_argument("--end", type=float, help="cut to, seconds (YouTube only)")
    g.set_defaults(func=cmd_get)

    h = sub.add_parser(
        "shuffle",
        help="crate-dig samplette-local's library (YouTube, with Discogs metadata)",
    )
    h.add_argument("-n", "--number", type=int, default=1)
    h.add_argument("--mode", choices=["shuffle", "popular", "recent"],
                   default="shuffle")
    h.add_argument("--genre", action="append", help="repeatable")
    h.add_argument("--style", action="append", help="repeatable")
    h.add_argument("--tag", action="append", help="repeatable")
    h.add_argument("--region", action="append", help="repeatable")
    h.add_argument("--key", action="append", help='e.g. "F minor", repeatable')
    h.add_argument("--tempo", help="BPM range: 90-150, 90-, -150, or 120")
    h.add_argument("--year", help="year range: 1970-1979")
    h.add_argument("--duration", help="seconds range: 60-300")
    h.add_argument("--query", help="text across artist / title / release")
    h.add_argument("--topic-only", action="store_true",
                   help='only auto-generated "- Topic" channels (cleanest audio)')
    h.add_argument("--get", action="store_true", help="download what comes up")
    h.add_argument("--format", default=config.WAV_48_24.key)
    h.add_argument("--out", help="output directory")
    h.add_argument("--stats", action="store_true", help="library size and coverage")
    h.add_argument("--facets", action="store_true", help="what you can filter on")
    h.set_defaults(func=cmd_shuffle)

    st = sub.add_parser("stems", help="split a local file into stems with UVR")
    st.add_argument("input", nargs="?", default="")
    st.add_argument("--pick", default="vocals,instrumental",
                    help="comma-separated stem options (see --list)")
    st.add_argument("--artist", help="artist name for the output filenames")
    st.add_argument("--out", help="output directory")
    st.add_argument("--list", action="store_true",
                    help="show every option and this machine's measured speed")
    st.set_defaults(func=cmd_stems)

    ph = sub.add_parser(
        "phrase",
        help="find spoken words inside YouTube videos and cut them out",
    )
    ph.add_argument("phrase")
    ph.add_argument("--candidates", type=int, default=config.PHRASE_CANDIDATES,
                    help="how many search results to read captions for")
    ph.add_argument("--limit", type=int, default=10, help="hits to show")
    ph.add_argument("--exact", action="store_true", help="no fuzzy matching")
    ph.add_argument("--get", type=int, metavar="N", help="download hit N")
    ph.add_argument("--format", default=config.WAV_48_24.key)
    ph.add_argument("--out", help="output directory")
    ph.add_argument("--no-trim", action="store_true",
                    help="keep the padded cut, skip silence-detect")
    ph.set_defaults(func=cmd_phrase)

    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--cookies", help="path to a cookies file you exported yourself")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    paths = config.Paths.default().ensure()
    cache = None if args.no_cache else Cache(paths.cache_db)
    engine = Engine(cache=cache, cookie_file=Path(args.cookies) if args.cookies else None)

    try:
        return args.func(args, engine)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    finally:
        if cache is not None:
            cache.close()
