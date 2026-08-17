"""A small, dependency-light client for the slskd Soulseek daemon.

slskd (https://github.com/slskd/slskd) speaks the Soulseek protocol and exposes
a REST API on top of it. This module wraps the handful of endpoints an app
actually needs: search, pick a result, download it, wait for it to land.

    from soulseek_api import SoulseekClient

    with SoulseekClient.from_env() as sk:
        files = sk.search("aphex twin xtal", timeout=20)
        best = sk.best_match(files, prefer_extensions=["flac", "mp3"])
        transfer = sk.download_and_wait(best)

Downloaded files land in slskd's own downloads directory (``downloads`` in
slskd.yml), not in the calling process's working directory.
"""

import os
import time
import uuid

import requests

from .errors import (
    APIError,
    AuthenticationFailed,
    ConnectionFailed,
    DownloadFailed,
    SearchTimeout,
)
from .models import SearchFile, SearchResponse, Transfer

DEFAULT_URL = "http://localhost:5030"
API_PREFIX = "/api/v0"

#: Errors that mean "slskd could not service this call" — used where a failure
#: is not worth propagating (best-effort cleanup, health checks).
TRANSPORT_ERRORS = (APIError, ConnectionFailed, AuthenticationFailed)


class SoulseekClient:
    """Synchronous client for one slskd instance.

    Auth is either an API key (``api_key``, configured in slskd.yml under
    ``web.authentication.api_keys``) or a username/password pair, which is
    exchanged for a bearer token on first use.
    """

    def __init__(self, url=DEFAULT_URL, api_key=None, username=None, password=None,
                 timeout=30, verify_ssl=True):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.username = username
        self.password = password
        self.timeout = timeout
        self._token = None
        self._session = requests.Session()
        self._session.verify = verify_ssl

    @classmethod
    def from_env(cls, **overrides):
        """Build a client from SLSKD_URL / SLSKD_API_KEY / SLSKD_USERNAME / SLSKD_PASSWORD."""
        config = {
            "url": os.environ.get("SLSKD_URL", DEFAULT_URL),
            "api_key": os.environ.get("SLSKD_API_KEY"),
            "username": os.environ.get("SLSKD_USERNAME"),
            "password": os.environ.get("SLSKD_PASSWORD"),
        }
        config.update(overrides)
        return cls(**config)

    # ------------------------------------------------------------------
    # plumbing
    # ------------------------------------------------------------------

    def _headers(self):
        if self.api_key:
            return {"X-API-Key": self.api_key}
        if self._token is None:
            self._login()
        return {"Authorization": f"Bearer {self._token}"}

    def _login(self):
        if not (self.username and self.password):
            raise AuthenticationFailed(
                "No credentials: set api_key, or username and password."
            )
        try:
            response = self._session.post(
                f"{self.url}{API_PREFIX}/session",
                json={"username": self.username, "password": self.password},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise ConnectionFailed(f"Cannot reach slskd at {self.url}: {exc}") from exc

        if response.status_code in (401, 403):
            raise AuthenticationFailed("slskd rejected the username/password.")
        if not response.ok:
            raise APIError(response.status_code, response.text, response.url)

        self._token = response.json().get("token")
        if not self._token:
            raise AuthenticationFailed("slskd did not return a session token.")

    def _request(self, method, path, **kwargs):
        kwargs.setdefault("timeout", self.timeout)
        url = f"{self.url}{API_PREFIX}{path}"
        try:
            response = self._session.request(
                method, url, headers=self._headers(), **kwargs
            )
        except requests.RequestException as exc:
            raise ConnectionFailed(f"Cannot reach slskd at {self.url}: {exc}") from exc

        if response.status_code in (401, 403):
            # A bearer token may simply have expired; retry once with a fresh one.
            if not self.api_key and self._token is not None:
                self._token = None
                response = self._session.request(
                    method, url, headers=self._headers(), **kwargs
                )
            if response.status_code in (401, 403):
                raise AuthenticationFailed(f"Not authorized for {path}.")

        if not response.ok:
            raise APIError(response.status_code, response.text, url)

        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------

    def ping(self):
        """True if slskd answers and is logged in to the Soulseek network."""
        try:
            state = self.state()
        except TRANSPORT_ERRORS:
            return False
        return str(state.get("server", {}).get("state", "")).find("Connected") >= 0

    def state(self):
        """Full application state (server connection, shares, versions...)."""
        return self._request("GET", "/application")

    # ------------------------------------------------------------------
    # searching
    # ------------------------------------------------------------------

    def search(self, query, timeout=30, file_limit=1000, min_bitrate=None,
               extensions=None, audio_only=False, poll_interval=1.0, cleanup=True):
        """Run a search and return a flat, sorted list of ``SearchFile``.

        Results are sorted by free upload slot first, then upload speed, so the
        head of the list is what is actually likely to download quickly.
        """
        search_id = self.start_search(query, file_limit=file_limit)
        try:
            self.wait_for_search(search_id, timeout=timeout, poll_interval=poll_interval)
            responses = self.get_responses(search_id)
        finally:
            if cleanup:
                try:
                    self.delete_search(search_id)
                except TRANSPORT_ERRORS:
                    pass  # a leftover search record is harmless

        files = [f for response in responses for f in response.files]
        return self.filter_files(
            files,
            min_bitrate=min_bitrate,
            extensions=extensions,
            audio_only=audio_only,
        )

    def start_search(self, query, file_limit=1000, search_timeout=15000):
        """Kick off a search without waiting; returns the search id."""
        search_id = str(uuid.uuid4())
        self._request(
            "POST",
            "/searches",
            json={
                "id": search_id,
                "searchText": query,
                "fileLimit": file_limit,
                "filterResponses": True,
                "searchTimeout": search_timeout,
            },
        )
        return search_id

    def get_search(self, search_id):
        return self._request("GET", f"/searches/{search_id}")

    def wait_for_search(self, search_id, timeout=30, poll_interval=1.0):
        """Block until slskd marks the search complete, or raise SearchTimeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            search = self.get_search(search_id)
            if "Completed" in str(search.get("state", "")):
                return search
            time.sleep(poll_interval)
        raise SearchTimeout(f"Search {search_id} did not complete in {timeout}s.")

    def get_responses(self, search_id):
        """Per-user responses for a search, as ``SearchResponse`` objects."""
        payload = self._request("GET", f"/searches/{search_id}/responses") or []
        return [SearchResponse.from_json(item) for item in payload]

    def delete_search(self, search_id):
        self._request("DELETE", f"/searches/{search_id}")

    # ------------------------------------------------------------------
    # picking a result
    # ------------------------------------------------------------------

    @staticmethod
    def filter_files(files, min_bitrate=None, extensions=None, audio_only=False,
                     min_size=None, free_slot_only=False):
        """Filter and rank search results. Best candidates come first."""
        wanted = None
        if extensions:
            wanted = {e.lower().lstrip(".") for e in extensions}

        kept = []
        for f in files:
            ext = f.extension.lower().lstrip(".")
            if audio_only and not f.is_audio:
                continue
            if wanted and ext not in wanted:
                continue
            # Bitrate is often absent from results; absent is not a failure.
            if min_bitrate and f.bitrate and f.bitrate < min_bitrate:
                continue
            if min_size and f.size < min_size:
                continue
            if free_slot_only and not f.free_upload_slot:
                continue
            kept.append(f)

        kept.sort(key=lambda f: (not f.free_upload_slot, -f.upload_speed, f.queue_length))
        return kept

    @staticmethod
    def best_match(files, prefer_extensions=None):
        """Pick one file, honouring a format preference order (e.g. flac then mp3)."""
        if not files:
            return None
        if prefer_extensions:
            for extension in prefer_extensions:
                extension = extension.lower().lstrip(".")
                for f in files:
                    if f.extension.lower().lstrip(".") == extension:
                        return f
        return files[0]

    # ------------------------------------------------------------------
    # downloading
    # ------------------------------------------------------------------

    def download(self, file, username=None, filename=None, size=None):
        """Enqueue a download.

        Accepts a ``SearchFile``, or an explicit username/filename/size trio.
        Returns the ``Transfer`` slskd created for it.
        """
        if isinstance(file, SearchFile):
            username, filename, size = file.username, file.filename, file.size
        if not (username and filename):
            raise ValueError("Need a SearchFile, or both username and filename.")

        self._request(
            "POST",
            f"/transfers/downloads/{username}",
            json=[{"filename": filename, "size": size or 0}],
        )
        # slskd returns 201 with no body, so read the transfer back to get its id.
        for transfer in self.get_downloads(username):
            if transfer.filename == filename:
                return transfer
        return Transfer(
            id="", username=username, filename=filename, size=size or 0,
            state="Requested",
        )

    def download_many(self, files):
        """Enqueue several files, batching per user. Returns the transfers."""
        by_user = {}
        for f in files:
            by_user.setdefault(f.username, []).append(f)

        transfers = []
        for username, user_files in by_user.items():
            self._request(
                "POST",
                f"/transfers/downloads/{username}",
                json=[{"filename": f.filename, "size": f.size} for f in user_files],
            )
            # One transfer per requested file: a filename can already appear in
            # the user's transfer list from an earlier, finished download.
            wanted = {f.filename for f in user_files}
            seen = set()
            for t in self.get_downloads(username):
                if t.filename in wanted and t.filename not in seen:
                    seen.add(t.filename)
                    transfers.append(t)
        return transfers

    def get_downloads(self, username=None):
        """Current downloads, for one user or all of them."""
        if username:
            payload = self._request("GET", f"/transfers/downloads/{username}")
            payload = [payload] if payload else []
        else:
            payload = self._request("GET", "/transfers/downloads") or []
        return self._flatten_transfers(payload)

    @staticmethod
    def _flatten_transfers(payload):
        """slskd nests transfers as user -> directories -> files."""
        transfers = []
        for user in payload:
            for directory in user.get("directories", []):
                for item in directory.get("files", []):
                    item.setdefault("username", user.get("username", ""))
                    transfers.append(Transfer.from_json(item))
        return transfers

    def wait_for_download(self, transfer, timeout=600, poll_interval=2.0,
                          on_progress=None):
        """Block until a transfer finishes. Raises DownloadFailed if it did not succeed."""
        deadline = time.monotonic() + timeout
        current = transfer
        while time.monotonic() < deadline:
            matches = [
                t for t in self.get_downloads(transfer.username)
                if t.filename == transfer.filename
            ]
            if matches:
                current = matches[0]
                if on_progress:
                    on_progress(current)
                if current.is_finished:
                    if not current.is_successful:
                        raise DownloadFailed(
                            f"{current.basename} ended in state '{current.state}'."
                        )
                    return current
            time.sleep(poll_interval)
        raise DownloadFailed(
            f"{current.basename} did not finish within {timeout}s "
            f"(last state: {current.state})."
        )

    def download_and_wait(self, file, timeout=600, poll_interval=2.0, on_progress=None):
        """Enqueue one file and block until slskd reports it succeeded."""
        transfer = self.download(file)
        return self.wait_for_download(
            transfer, timeout=timeout, poll_interval=poll_interval,
            on_progress=on_progress,
        )

    def cancel_download(self, transfer, remove=True):
        """Cancel a transfer; ``remove`` also drops it from the transfer list."""
        self._request(
            "DELETE",
            f"/transfers/downloads/{transfer.username}/{transfer.id}",
            params={"remove": str(remove).lower()},
        )

    # ------------------------------------------------------------------
    # users and browsing
    # ------------------------------------------------------------------

    def user_info(self, username):
        return self._request("GET", f"/users/{username}/info")

    def user_status(self, username):
        return self._request("GET", f"/users/{username}/status")

    def browse(self, username):
        """List everything a user shares."""
        return self._request("GET", f"/users/{username}/browse") or []

    def browse_directory(self, username, directory):
        """List one directory from a user's shares — handy for grabbing a whole album."""
        return self._request(
            "POST", f"/users/{username}/directory", json={"directory": directory}
        )

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def close(self):
        self._session.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
