"""Getting slskd onto this machine, configured, and running.

Soulseek has no official HTTP API — it is a proprietary TCP protocol — so the
standard way to script it is slskd, a headless client that logs into the
network and exposes REST. `soulseek/soulseek_api` talks to that.

Worth being clear about, because it surprises people: the official Soulseek
desktop client and slskd are different programs. Having one installed does not
provide the other. They share an account, and Soulseek permits one login per
account, so starting slskd will generally disconnect a desktop client logged
in as the same user.

Nothing here downloads anything until asked. The archive is fetched on first
use of the Soulseek tab, not at install time, and an slskd already on the
machine is used in preference to fetching another.
"""

from __future__ import annotations

import hashlib
import logging
import os
import platform
import secrets
import shutil
import signal
import subprocess
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

import yaml

from .. import config

log = logging.getLogger(__name__)

ProgressFn = Callable[[float, str], None]

VERSION = "0.26.0"
RELEASE_URL = (
    "https://github.com/slskd/slskd/releases/download/{version}/"
    "slskd-{version}-{platform}.zip"
)
#: Published sizes, checked against what actually arrives. slskd publishes no
#: checksum file, so there is nothing authoritative to pin a hash against —
#: pinning one computed here would only prove the download matched whatever
#: was fetched on the day this was written, which is worth less than it looks.
#: Transport integrity comes from HTTPS to github.com; this is a sanity check
#: against a truncated or redirected download, and is described as such.
EXPECTED_SIZES = {
    "osx-arm64": 58_300_000,
    "osx-x64": 60_600_000,
}
SIZE_TOLERANCE = 0.15

DEFAULT_PORT = 5030
#: Bound to loopback only. The daemon is an implementation detail of this app
#: and has no business being reachable from the network.
BIND_ADDRESS = "127.0.0.1"


class SlskdError(RuntimeError):
    pass


def platform_key() -> str:
    if platform.system() != "Darwin":
        raise SlskdError(f"unsupported platform {platform.system()}")
    return "osx-arm64" if platform.machine() == "arm64" else "osx-x64"


@dataclass
class SlskdStatus:
    installed: bool
    running: bool
    configured: bool
    port: int
    binary: Path | None = None
    detail: str = ""
    #: True when something else is already serving on the port — the user
    #: running their own slskd, most likely.
    foreign: bool = False


class SlskdBootstrap:
    """Downloads, configures, starts and stops slskd."""

    def __init__(
        self,
        paths: config.Paths | None = None,
        port: int = DEFAULT_PORT,
        session: Any | None = None,
    ) -> None:
        self.paths = paths or config.Paths.default()
        self.port = port
        self._session = session
        self._process: subprocess.Popen | None = None

    # -- locations --------------------------------------------------------

    @property
    def home(self) -> Path:
        return self.paths.slskd_dir

    @property
    def binary(self) -> Path:
        return self.home / "slskd"

    @property
    def config_file(self) -> Path:
        return self.home / "slskd.yml"

    @property
    def api_key_file(self) -> Path:
        return self.home / "api_key"

    @property
    def url(self) -> str:
        return f"http://{BIND_ADDRESS}:{self.port}"

    def find_existing(self) -> Path | None:
        """An slskd the user installed themselves, if there is one.

        Preferred over downloading another copy: they may have configured it
        already, and two 58 MB binaries on one machine helps nobody.
        """
        found = shutil.which("slskd")
        return Path(found) if found else None

    def resolve_binary(self) -> Path | None:
        if self.binary.exists():
            return self.binary
        return self.find_existing()

    def installed(self) -> bool:
        return self.resolve_binary() is not None

    # -- download ---------------------------------------------------------

    def download_url(self) -> str:
        return RELEASE_URL.format(version=VERSION, platform=platform_key())

    def install(
        self,
        *,
        progress: ProgressFn | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> Path:
        """Fetch and unpack slskd. Roughly 58 MB.

        Self-contained: the osx-arm64 build needs neither Homebrew nor a .NET
        runtime, which is what makes it usable on this machine at all.
        """
        import requests

        session = self._session or requests
        key = platform_key()
        url = self.download_url()
        self.home.mkdir(parents=True, exist_ok=True)
        archive = self.home / f"slskd-{VERSION}-{key}.zip"

        if progress:
            progress(0.0, "downloading slskd")

        try:
            response = session.get(url, stream=True, timeout=60)
            response.raise_for_status()
            total = int(response.headers.get("content-length") or 0)
            written = 0
            digest = hashlib.sha256()
            with archive.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1 << 16):
                    if should_cancel and should_cancel():
                        archive.unlink(missing_ok=True)
                        raise SlskdError("cancelled")
                    handle.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
                    if progress and total:
                        progress(0.85 * written / total, "downloading slskd")
        except SlskdError:
            raise
        except Exception as exc:  # noqa: BLE001
            archive.unlink(missing_ok=True)
            raise SlskdError(f"could not download slskd: {exc}") from exc

        expected = EXPECTED_SIZES.get(key)
        if expected and abs(written - expected) > expected * SIZE_TOLERANCE:
            archive.unlink(missing_ok=True)
            raise SlskdError(
                f"slskd download is {written / 1e6:.1f} MB, expected about "
                f"{expected / 1e6:.0f} MB — refusing to unpack it"
            )
        log.info("slskd %s sha256 %s", VERSION, digest.hexdigest())

        if progress:
            progress(0.9, "unpacking")
        try:
            self._extract(archive)
        finally:
            archive.unlink(missing_ok=True)

        if not self.binary.exists():
            raise SlskdError("the archive contained no slskd executable")
        self.binary.chmod(0o755)
        if progress:
            progress(1.0, "installed")
        return self.binary

    def _extract(self, archive: Path) -> None:
        with zipfile.ZipFile(archive) as zf:
            for member in zf.infolist():
                name = Path(member.filename)
                # A zip entry may name any path it likes; refuse anything that
                # would land outside the install directory.
                if member.filename.startswith("/") or ".." in name.parts:
                    raise SlskdError(f"unsafe path in archive: {member.filename}")
            zf.extractall(self.home)

        if self.binary.exists():
            return
        # Some builds nest everything one directory deep.
        for candidate in self.home.rglob("slskd"):
            if candidate.is_file():
                candidate.replace(self.binary)
                return

    # -- configuration ----------------------------------------------------

    def ensure_api_key(self) -> str:
        if self.api_key_file.exists():
            key = self.api_key_file.read_text("utf-8").strip()
            if key:
                return key
        key = secrets.token_hex(24)
        self.home.mkdir(parents=True, exist_ok=True)
        self.api_key_file.write_text(key, "utf-8")
        self.api_key_file.chmod(0o600)
        return key

    def write_config(
        self,
        username: str,
        password: str,
        share_dirs: Sequence[Path | str],
        download_dir: Path | str | None = None,
        api_key: str | None = None,
    ) -> Path:
        """Write slskd.yml.

        The credentials land in this file because slskd has no other way to
        receive them. It is written 0600 inside the app's own support
        directory — not in the repo, and not anywhere synced by default.
        NEYTA's copy of record stays in the Keychain.
        """
        if not username or not password:
            raise SlskdError("Soulseek needs a username and a password")
        shares = [str(Path(d).expanduser().resolve()) for d in share_dirs if d]
        if not shares:
            raise SlskdError(
                "Soulseek needs a folder to share. The network bans clients "
                "that only take, so point this at real music."
            )
        for share in shares:
            if not Path(share).is_dir():
                raise SlskdError(f"shared folder does not exist: {share}")

        downloads = Path(download_dir or (self.paths.downloads / "Soulseek"))
        downloads.mkdir(parents=True, exist_ok=True)
        incomplete = self.paths.cache / "slskd-incomplete"
        incomplete.mkdir(parents=True, exist_ok=True)

        key = api_key or self.ensure_api_key()
        self.home.mkdir(parents=True, exist_ok=True)

        document = {
            "soulseek": {"username": username, "password": password},
            "shares": {"directories": shares},
            "directories": {
                "downloads": str(downloads),
                "incomplete": str(incomplete),
            },
            "web": {
                "port": self.port,
                "url_base": "/",
                "authentication": {
                    "api_keys": {
                        "neyta": {
                            "key": key,
                            # Loopback only. This daemon is an implementation
                            # detail of NEYTA and is never reachable beyond
                            # this machine.
                            "cidr": f"{BIND_ADDRESS}/32,::1/128",
                        }
                    }
                },
            },
        }
        # Serialised by PyYAML rather than by hand. A password may contain
        # quotes, colons, backslashes or leading whitespace, and a home-made
        # quoter is one edge case away from writing a config slskd silently
        # misreads as a different password.
        header = ("# Written by NEYTA. Edits here are overwritten when the\n"
                  "# Soulseek credentials change in Settings.\n")
        self.config_file.write_text(
            header + yaml.safe_dump(document, sort_keys=False), "utf-8"
        )
        self.config_file.chmod(0o600)
        return self.config_file

    def configured(self) -> bool:
        return self.config_file.exists() and self.api_key_file.exists()

    def api_key(self) -> str | None:
        if self.api_key_file.exists():
            return self.api_key_file.read_text("utf-8").strip() or None
        return None

    # -- supervision ------------------------------------------------------

    def port_in_use(self) -> bool:
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            return sock.connect_ex((BIND_ADDRESS, self.port)) == 0

    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(self, *, wait: float = 25.0) -> None:
        """Start slskd as a supervised child and wait for it to answer."""
        if self.is_running():
            return
        binary = self.resolve_binary()
        if binary is None:
            raise SlskdError("slskd is not installed yet")
        if not self.config_file.exists():
            raise SlskdError("slskd is not configured — set your Soulseek login")
        if self.port_in_use():
            # Someone else's slskd, or a leftover of ours. Either way, do not
            # start a second one fighting for the same port and account.
            raise SlskdError(
                f"something is already listening on {BIND_ADDRESS}:{self.port}. "
                "If that is your own slskd, NEYTA can use it as-is."
            )

        env = dict(os.environ)
        env["SLSKD_APP_DIR"] = str(self.home)
        self._process = subprocess.Popen(
            [str(binary), "--config", str(self.config_file),
             "--app-dir", str(self.home)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            env=env, start_new_session=True,
        )

        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                stderr = (self._process.stderr.read().decode("utf-8", "replace")
                          if self._process.stderr else "")
                self._process = None
                raise SlskdError(f"slskd exited immediately:\n{stderr[-600:]}")
            if self.port_in_use():
                return
            time.sleep(0.4)

        self.stop()
        raise SlskdError(f"slskd did not come up within {wait:.0f}s")

    def stop(self, timeout: float = 10.0) -> None:
        """Shut the daemon down cleanly. Called when the app quits."""
        process = self._process
        self._process = None
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    # -- status -----------------------------------------------------------

    def status(self) -> SlskdStatus:
        binary = self.resolve_binary()
        in_use = self.port_in_use()
        foreign = in_use and not self.is_running()

        if binary is None:
            detail = "not installed — NEYTA can fetch it (about 58 MB)"
        elif not self.configured():
            detail = "installed, but no Soulseek login yet"
        elif self.is_running():
            detail = f"running on {self.url}"
        elif foreign:
            detail = f"something else is serving {self.url}"
        else:
            detail = "ready to start"

        return SlskdStatus(
            installed=binary is not None,
            running=self.is_running() or foreign,
            configured=self.configured(),
            port=self.port,
            binary=binary,
            detail=detail,
            foreign=foreign,
        )

    def __enter__(self) -> "SlskdBootstrap":
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
