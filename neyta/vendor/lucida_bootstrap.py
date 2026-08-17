"""Finding lucida-flow on this machine, and running its local server.

The Spotify tab is a client of lucida-flow (github.com/ryanlong1004/lucida-flow),
which drives lucida.to through a headless browser and puts a small HTTP API in
front of it. Same arrangement as slskd behind the Soulseek tab: NEYTA speaks
HTTP to a local process rather than importing anything, so playwright, chromium
and FastAPI stay in that project's interpreter and never enter this one.

Nothing here installs anything. slskd is a single signed binary NEYTA can fetch
with a size check; lucida-flow is a source checkout with a browser engine behind
it, and downloading and building that on someone's behalf is not a thing this
app should do quietly. So the bootstrap looks for a checkout, tells you exactly
what is missing when there is not one, and starts it when there is.

The server binds to loopback. It is an implementation detail of this app and has
no business being reachable from the network.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .. import config

log = logging.getLogger(__name__)

BIND_ADDRESS = "127.0.0.1"

#: What to tell someone who has not set it up. One paragraph, with the actual
#: commands in it, because "not installed" on its own is not actionable.
SETUP_HINT = (
    "The Spotify tab needs lucida-flow beside NEYTA:\n\n"
    "    git clone https://github.com/ryanlong1004/lucida-flow {root}\n"
    "    python3 -m venv {root}/.venv\n"
    "    {root}/.venv/bin/pip install -r {root}/requirements.txt\n"
    "    {root}/.venv/bin/playwright install chromium\n"
)


class LucidaError(RuntimeError):
    pass


@dataclass
class LucidaStatus:
    installed: bool
    running: bool
    port: int
    root: Path
    detail: str = ""
    #: True when something else already answers on the port — the user running
    #: their own copy, most likely, which NEYTA can simply use.
    foreign: bool = False


class LucidaBootstrap:
    """Locates the checkout, starts and stops its API server."""

    def __init__(
        self,
        root: Path | None = None,
        python: Path | None = None,
        port: int = config.LUCIDA_PORT,
    ) -> None:
        self.root = Path(root or config.LUCIDA_ROOT)
        self._python = Path(python) if python else None
        self.port = port
        self._process: subprocess.Popen | None = None

    # -- locations --------------------------------------------------------

    @property
    def server(self) -> Path:
        return self.root / "api_server.py"

    @property
    def python(self) -> Path:
        """The checkout's own interpreter, or whatever it was given.

        Its venv first, because that is where playwright will have been
        installed; a bare `python3` would import a lucida-flow whose
        dependencies are not there.
        """
        if self._python is not None:
            return self._python
        venv = self.root / ".venv" / "bin" / "python"
        return venv if venv.exists() else Path("python3")

    @property
    def url(self) -> str:
        return f"http://{BIND_ADDRESS}:{self.port}"

    def installed(self) -> bool:
        return self.server.exists()

    @property
    def setup_hint(self) -> str:
        return SETUP_HINT.format(root=self.root)

    # -- supervision ------------------------------------------------------

    def port_in_use(self) -> bool:
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            return sock.connect_ex((BIND_ADDRESS, self.port)) == 0

    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(self, *, wait: float = 30.0) -> None:
        """Start the API server as a supervised child and wait for the port.

        Slower to come up than most things NEYTA starts — it imports
        playwright — so the wait is generous rather than optimistic.
        """
        if self.is_running() or self.port_in_use():
            return
        if not self.installed():
            raise LucidaError(self.setup_hint)

        env = dict(os.environ)
        env["API_HOST"] = BIND_ADDRESS
        env["API_PORT"] = str(self.port)
        # Its downloads land in NEYTA's cache, not in the checkout: they are
        # intermediates on the way to the download folder, and the export
        # dialog decides where the finished file goes.
        env["DOWNLOAD_DIR"] = str(self.download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)

        self._process = subprocess.Popen(
            [str(self.python), str(self.server)],
            cwd=str(self.root),
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            env=env, start_new_session=True,
        )

        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                stderr = (self._process.stderr.read().decode("utf-8", "replace")
                          if self._process.stderr else "")
                self._process = None
                raise LucidaError(
                    f"lucida-flow exited immediately:\n{stderr[-600:]}"
                )
            if self.port_in_use():
                return
            time.sleep(0.4)

        self.stop()
        raise LucidaError(f"lucida-flow did not come up within {wait:.0f}s")

    @property
    def download_dir(self) -> Path:
        return config.Paths.default().cache / "lucida"

    def stop(self, timeout: float = 10.0) -> None:
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

    def status(self) -> LucidaStatus:
        in_use = self.port_in_use()
        foreign = in_use and not self.is_running()
        if not self.installed():
            detail = f"not found at {self.root}"
        elif self.is_running():
            detail = f"running on {self.url}"
        elif foreign:
            detail = f"something else is serving {self.url}"
        else:
            detail = "ready to start"
        return LucidaStatus(
            installed=self.installed(),
            running=self.is_running() or foreign,
            port=self.port,
            root=self.root,
            detail=detail,
            foreign=foreign,
        )

    def __enter__(self) -> "LucidaBootstrap":
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
