"""QApplication bootstrap.

Owns the objects that outlive any one window — settings, the job queue, the
cache — and hands them to the UI. The window itself arrives in phase 3; until
then `main()` says so plainly rather than opening an empty frame.
"""

from __future__ import annotations

import logging
import sys

from . import config
from .core.cache import Cache
from .core.jobs import JobQueue
from .settings import Settings

log = logging.getLogger(__name__)


class Services:
    """Everything the UI needs, constructed once and injected.

    Nothing here reaches for a global; tests build a Services against a
    tmp_path and get a fully isolated app.
    """

    def __init__(self, paths: config.Paths | None = None, *, native: bool = True):
        self.paths = (paths or config.Paths.default()).ensure()
        self.settings = (
            Settings.native(self.paths) if native else Settings.headless(self.paths)
        )
        self.cache = Cache(self.paths.cache_db)
        self.queue = JobQueue(workers=4)

    def shutdown(self) -> None:
        self.queue.shutdown(wait=False)
        self.cache.close()


def build_application(argv: list[str] | None = None):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    QApplication.setApplicationName(config.APP_NAME)
    QApplication.setOrganizationName(config.APP_ORG)
    QApplication.setAttribute(Qt.AA_DontShowIconsInMenus, False)

    app = QApplication.instance() or QApplication(argv or sys.argv)
    app.setQuitOnLastWindowClosed(True)
    return app


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    app = build_application(argv)
    services = Services()

    try:
        from .ui.window import MainWindow
    except ImportError:
        print(
            "The window is built in phase 3. Phase 1 is complete: run\n"
            "  python -m neyta doctor    environment self-check\n"
            "  python -m pytest          the unit suite",
            file=sys.stderr,
        )
        services.shutdown()
        return 2

    window = MainWindow(services)
    window.show()
    # After show(), so the app is visibly there behind it rather than the
    # first thing on screen being a modal.
    window.maybe_welcome()
    try:
        return app.exec()
    finally:
        services.shutdown()
