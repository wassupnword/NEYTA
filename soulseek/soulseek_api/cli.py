"""Command line front end, mostly for checking that the setup works.

    python -m soulseek_api.cli status
    python -m soulseek_api.cli search "aphex twin xtal" --audio-only
    python -m soulseek_api.cli get "boards of canada roygbiv" --format flac
"""

import argparse
import sys

from .client import SoulseekClient
from .errors import SoulseekError


def _client(args):
    return SoulseekClient.from_env(url=args.url) if args.url else SoulseekClient.from_env()


def cmd_status(args):
    with _client(args) as sk:
        state = sk.state()
        server = state.get("server", {})
        print(f"slskd      : {state.get('version', 'unknown')}")
        print(f"server     : {server.get('state', 'unknown')}")
        print(f"username   : {server.get('username', '-')}")
        shared = state.get("shares", {}).get("directories", "-")
        print(f"shared dirs: {shared}")
    return 0


def cmd_search(args):
    with _client(args) as sk:
        files = sk.search(
            args.query,
            timeout=args.timeout,
            audio_only=args.audio_only,
            extensions=args.format,
            min_bitrate=args.min_bitrate,
        )
        if not files:
            print("No results.")
            return 1
        for index, f in enumerate(files[: args.limit], 1):
            slot = "free" if f.free_upload_slot else f"q{f.queue_length}"
            print(f"{index:3}. [{slot}] {f}")
        print(f"\n{len(files)} result(s), showing {min(args.limit, len(files))}.")
    return 0


def cmd_get(args):
    with _client(args) as sk:
        files = sk.search(
            args.query,
            timeout=args.timeout,
            audio_only=True,
            extensions=args.format,
            min_bitrate=args.min_bitrate,
        )
        best = sk.best_match(files, prefer_extensions=args.format)
        if best is None:
            print("No results.")
            return 1

        print(f"Downloading: {best}")
        last = [-1]

        def progress(transfer):
            percent = int(transfer.percent)
            if percent != last[0]:
                last[0] = percent
                print(f"\r  {percent:3d}%  {transfer.state}", end="", flush=True)

        transfer = sk.download_and_wait(
            best, timeout=args.download_timeout, on_progress=progress
        )
        print(f"\nDone: {transfer.basename}")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="soulseek_api", description="Talk to a slskd Soulseek daemon."
    )
    parser.add_argument("--url", help="slskd base URL (default: $SLSKD_URL)")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="show daemon and network status")
    status.set_defaults(func=cmd_status)

    def add_query_parser(name, handler, help_text):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("query")
        p.add_argument("--timeout", type=int, default=30,
                       help="seconds to let the search run")
        p.add_argument("--format", action="append",
                       help="restrict to an extension; repeat for a preference order")
        p.add_argument("--min-bitrate", type=int, help="drop results below this kbps")
        p.set_defaults(func=handler)
        return p

    search = add_query_parser("search", cmd_search, "search and list results")
    search.add_argument("--limit", type=int, default=25, help="how many results to print")
    search.add_argument("--audio-only", action="store_true", help="drop non-audio files")

    get = add_query_parser(
        "get", cmd_get, "search, pick the best result, and download it"
    )
    get.add_argument("--download-timeout", type=int, default=600,
                     help="seconds to wait for the transfer")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except SoulseekError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
