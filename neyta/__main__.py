"""`python -m neyta [doctor]`"""

from __future__ import annotations

import argparse
import sys


CLI_COMMANDS = {"search", "formats", "get", "shuffle", "stems", "phrase"}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    if argv and argv[0] in CLI_COMMANDS:
        from .cli import main as run_cli

        return run_cli(argv)

    parser = argparse.ArgumentParser(
        prog="neyta",
        epilog="engine commands: search, formats, get — try `neyta search --help`",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("doctor", help="check this machine's environment")
    sub.add_parser("run", help="launch the app (default)")

    args = parser.parse_args(argv)

    if args.command == "doctor":
        from .doctor import report

        return report()

    from .app import main as run_app

    return run_app()


if __name__ == "__main__":
    sys.exit(main())
