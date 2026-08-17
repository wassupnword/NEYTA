#!/usr/bin/env python3
"""Launcher: start the local server and open the app in your browser.

    python3 run.py            # start, open a browser tab
    python3 run.py --no-open  # start without opening a tab
    python3 run.py --port N   # use a different port
"""
import argparse
import sys
import threading
import time
import webbrowser

from app.config import HOST, PORT


def main() -> int:
    parser = argparse.ArgumentParser(description="Samplette Local")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--no-open", action="store_true",
                        help="don't open a browser tab")
    args = parser.parse_args()

    try:
        import uvicorn
    except ImportError:
        print("Dependencies are missing. Run ./run.sh instead, or:\n"
              "  pip install -r requirements.txt", file=sys.stderr)
        return 1

    url = "http://{}:{}".format(
        "localhost" if args.host in ("127.0.0.1", "0.0.0.0") else args.host,
        args.port)

    if not args.no_open:
        def open_later() -> None:
            # Give uvicorn a moment to bind before the tab races it.
            time.sleep(1.2)
            webbrowser.open(url)
        threading.Thread(target=open_later, daemon=True).start()

    print("\n  Samplette Local  →  {}\n".format(url))
    print("  The catalog builds itself in the background. First tracks appear")
    print("  within a minute or so; it keeps growing while you listen.")
    print("  Ctrl+C to stop.\n")

    uvicorn.run("app.main:app", host=args.host, port=args.port,
                log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
