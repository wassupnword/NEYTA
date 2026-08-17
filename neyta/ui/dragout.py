"""The drag mechanism.

This is the whole reason NEYTA is a native app rather than a browser tab.
Dragging a stem into a Live arrangement works exactly as dragging from Finder
does, because at the pasteboard level it is the same thing: a `text/uri-list`
of real `file://` paths.

Ableton and Finder both read that flavour, so getting it exactly right matters
more than anything else in the UI — and it is the easiest thing to silently
break, which is why `build_mime` is a pure function with its own tests rather
than something buried in a widget's mouseMoveEvent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Sequence

from PySide6.QtCore import QMimeData, QPoint, QUrl, Qt
from PySide6.QtGui import QDrag, QFont, QPainter, QPixmap
from PySide6.QtWidgets import QApplication


def existing_paths(paths: Iterable[Path | str]) -> list[Path]:
    """Absolute, de-duplicated, and confirmed on disk, in the given order.

    A URI pointing at something that is not there drops silently into Live —
    no error, no clip, and no clue why. Better to drag four of five files than
    to hand over a list with a hole in it.
    """
    seen: set[Path] = set()
    out: list[Path] = []
    for raw in paths:
        path = Path(raw).expanduser()
        try:
            path = path.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if not path.is_file() or path in seen:
            continue
        seen.add(path)
        out.append(path)
    return out


def build_mime(paths: Sequence[Path | str]) -> QMimeData:
    """The pasteboard payload for one or more finished files.

    Sets both `text/uri-list` (what Finder and Live actually read) and plain
    text (so dropping into a text field or a terminal gives usable paths).
    """
    files = existing_paths(paths)
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(p)) for p in files])
    if files:
        mime.setText("\n".join(str(p) for p in files))
    return mime


def uri_list(mime: QMimeData) -> list[str]:
    """The URIs in a payload, for assertions and for round-trip tests."""
    return [u.toString() for u in mime.urls()]


def local_paths(mime: QMimeData) -> list[Path]:
    return [Path(u.toLocalFile()) for u in mime.urls() if u.isLocalFile()]


def drag_pixmap(count: int, label: str = "") -> QPixmap:
    """A small badge shown under the cursor while dragging.

    Purely cosmetic, but dragging with no visual means you cannot tell whether
    the drag started, and a failed drag looks identical to a click.
    """
    ratio = QApplication.instance().devicePixelRatio() if QApplication.instance() else 1.0
    text = label or (f"{count} files" if count != 1 else "1 file")
    width, height = max(90, 12 + 7 * len(text)), 26

    pixmap = QPixmap(int(width * ratio), int(height * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(Qt.black)
    painter.setOpacity(0.82)
    painter.drawRoundedRect(0, 0, width, height, 6, 6)
    painter.setOpacity(1.0)
    painter.setPen(Qt.white)
    font = QFont()
    font.setPointSize(11)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignCenter, text)
    painter.end()
    return pixmap


def start_drag(source, paths: Sequence[Path | str], label: str = "") -> Qt.DropAction:
    """Begin a copy drag of `paths` from `source`.

    Copy rather than move: the file stays in the download folder after it has
    been dragged into a session, which is what every other music tool does and
    what stops a drag from destroying the only copy.
    """
    files = existing_paths(paths)
    if not files:
        return Qt.IgnoreAction

    drag = QDrag(source)
    drag.setMimeData(build_mime(files))
    pixmap = drag_pixmap(len(files), label)
    drag.setPixmap(pixmap)
    drag.setHotSpot(QPoint(pixmap.width() // 2, pixmap.height() // 2))
    return drag.exec(Qt.CopyAction, Qt.CopyAction)
