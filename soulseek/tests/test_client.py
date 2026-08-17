"""Run the client against a stub slskd. No daemon, no network, no fixtures.

    python tests/test_client.py
"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from soulseek_api import (  # noqa: E402
    AuthenticationFailed,
    ConnectionFailed,
    DownloadFailed,
    SearchTimeout,
    SoulseekClient,
)

API_KEY = "test-key"
TOKEN = "test-token"
PASSWORD = "test-password"
NEVER_COMPLETES = "never-completes"

# Mutated by the handler so assertions can check what the client actually sent.
RECORDED = {"search_polls": {}, "queued": [], "download_state": "Completed, Succeeded"}


class StubHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, payload=None):
        body = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        return (
            self.headers.get("X-API-Key") == API_KEY
            or self.headers.get("Authorization") == f"Bearer {TOKEN}"
        )

    def _json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"null")

    def do_POST(self):
        if self.path == "/api/v0/session":
            body = self._json_body()
            if body.get("password") == PASSWORD:
                return self._send(200, {"token": TOKEN})
            return self._send(401, {})

        if not self._authorized():
            return self._send(401, {})

        if self.path == "/api/v0/searches":
            body = self._json_body()
            RECORDED["search_polls"][body["id"]] = 0
            return self._send(201, {"id": body["id"], "state": "InProgress"})

        if self.path.startswith("/api/v0/transfers/downloads/"):
            username = self.path.rsplit("/", 1)[-1]
            for item in self._json_body():
                RECORDED["queued"].append((username, item["filename"]))
            return self._send(201, None)

        self._send(404, {})

    def do_GET(self):
        if not self._authorized():
            return self._send(401, {})
        path = self.path

        if path == "/api/v0/application":
            return self._send(200, {
                "version": "0.21.0",
                "server": {"state": "Connected, LoggedIn", "username": "stub-user"},
                "shares": {"directories": 1},
            })

        if path.startswith("/api/v0/searches/") and path.endswith("/responses"):
            return self._send(200, [{
                "username": "peer1",
                "uploadSpeed": 900,
                "queueLength": 0,
                "hasFreeUploadSlot": True,
                "files": [
                    {"filename": "C:\\Mus\\track.flac", "size": 30000000,
                     "bitRate": 0, "length": 200, "extension": "flac"},
                    {"filename": "C:\\Mus\\track.mp3", "size": 5000000,
                     "bitRate": 320, "length": 200, "extension": "mp3"},
                    {"filename": "C:\\Mus\\cover.jpg", "size": 100000,
                     "bitRate": 0, "length": 0, "extension": "jpg"},
                ],
                "lockedFiles": [{"filename": "C:\\Mus\\locked.mp3", "size": 1}],
            }, {
                "username": "peer2",
                "uploadSpeed": 10,
                "queueLength": 7,
                "hasFreeUploadSlot": False,
                "files": [
                    {"filename": "/home/x/track.mp3", "size": 4000000,
                     "bitRate": 128, "length": 200, "extension": "mp3"},
                ],
            }])

        if path.startswith("/api/v0/searches/"):
            search_id = path.rsplit("/", 1)[-1]
            RECORDED["search_polls"][search_id] = \
                RECORDED["search_polls"].get(search_id, 0) + 1
            if search_id == NEVER_COMPLETES:
                return self._send(200, {"id": search_id, "state": "InProgress"})
            # Report InProgress once so the polling loop is genuinely exercised.
            state = ("Completed, TimedOut"
                     if RECORDED["search_polls"][search_id] > 1 else "InProgress")
            return self._send(200, {"id": search_id, "state": state})

        if path.startswith("/api/v0/transfers/downloads/"):
            username = path.rsplit("/", 1)[-1]
            files = [
                {"id": f"t{i}", "filename": filename, "size": 5000000,
                 "state": RECORDED["download_state"], "bytesTransferred": 5000000}
                for i, (user, filename) in enumerate(RECORDED["queued"])
                if user == username
            ]
            return self._send(200, {"username": username,
                                    "directories": [{"files": files}]})

        self._send(404, {})

    def do_DELETE(self):
        if not self._authorized():
            return self._send(401, {})
        self._send(204, None)


def start_stub():
    server = HTTPServer(("127.0.0.1", 0), StubHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def check(label, condition, detail=""):
    if not condition:
        raise AssertionError(f"{label} failed. {detail}")
    print(f"  ok  {label}")


def main():
    server, url = start_stub()
    try:
        print("api key auth")
        sk = SoulseekClient(url=url, api_key=API_KEY)
        check("ping reports connected", sk.ping() is True)

        files = sk.search("test", timeout=10, poll_interval=0.05, audio_only=True)
        check("non-audio and locked files dropped",
              [f.basename for f in files] == ["track.flac", "track.mp3", "track.mp3"],
              [f.basename for f in files])
        check("free-slot peer ranked first",
              files[0].username == "peer1" and files[-1].username == "peer2")
        check("per-user metadata copied onto files",
              files[0].upload_speed == 900 and files[0].free_upload_slot)
        check("posix filenames handled", files[-1].basename == "track.mp3")

        check("extension filter", all(
            f.extension == "mp3"
            for f in sk.filter_files(files, extensions=["mp3"])))
        check("min_bitrate keeps unknown bitrates",
              len(sk.filter_files(files, min_bitrate=200)) == 2)
        check("free_slot_only", all(
            f.free_upload_slot for f in sk.filter_files(files, free_slot_only=True)))

        best = sk.best_match(files, prefer_extensions=["mp3"])
        check("best_match honours preference", best.extension == "mp3")
        check("best_match falls back to rank",
              sk.best_match(files).basename == "track.flac")
        check("best_match on empty list", sk.best_match([]) is None)

        transfer = sk.download_and_wait(best, timeout=5, poll_interval=0.05)
        check("download succeeded", transfer.is_successful and transfer.percent == 100.0)
        check("correct file queued",
              RECORDED["queued"] == [("peer1", "C:\\Mus\\track.mp3")])
        sk.cancel_download(transfer)
        sk.close()

        print("username/password auth")
        sk2 = SoulseekClient(url=url, username="u", password=PASSWORD)
        check("token obtained on first call", sk2.ping() and sk2._token == TOKEN)
        transfers = sk2.download_many(files[:2])
        check("download_many returns one transfer per file", len(transfers) == 2,
              [t.filename for t in transfers])
        sk2.close()

        print("failure paths")
        RECORDED["download_state"] = "Completed, Errored"
        sk3 = SoulseekClient(url=url, api_key=API_KEY)
        try:
            sk3.wait_for_download(transfers[0], timeout=5, poll_interval=0.05)
            raise AssertionError("expected DownloadFailed")
        except DownloadFailed:
            check("errored transfer raises DownloadFailed", True)
        RECORDED["download_state"] = "Completed, Succeeded"

        try:
            sk3.wait_for_search(NEVER_COMPLETES, timeout=0.3, poll_interval=0.05)
            raise AssertionError("expected SearchTimeout")
        except SearchTimeout:
            check("slow search raises SearchTimeout", True)
        sk3.close()

        for label, client in (
            ("bad password", SoulseekClient(url=url, username="u", password="wrong")),
            ("bad api key", SoulseekClient(url=url, api_key="wrong")),
            ("no credentials", SoulseekClient(url=url)),
        ):
            try:
                client.state()
                raise AssertionError(f"expected AuthenticationFailed for {label}")
            except AuthenticationFailed:
                check(f"{label} rejected", True)
            client.close()

        dead = SoulseekClient(url="http://127.0.0.1:1", api_key=API_KEY, timeout=2)
        try:
            dead.state()
            raise AssertionError("expected ConnectionFailed")
        except ConnectionFailed:
            check("unreachable slskd raises ConnectionFailed", True)
        check("ping is False when unreachable", dead.ping() is False)
        dead.close()

        print("\nALL PASS")
        return 0
    finally:
        server.shutdown()


if __name__ == "__main__":
    sys.exit(main())
