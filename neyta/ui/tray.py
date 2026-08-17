"""The drag tray — the download folder, each file with a handle.

Group 2's page, and the whole of it: what you have already pulled down is a
different job from finding the next thing, so it gets the window rather than
a column beside the results.

It behaves like a Finder window pointed at that folder — a path across the
top, the files under it, and everything you can do to one behind a
right-click rather than in a row of buttons.

Multi-select drags several stems at once, which is what you want when a
separation has just produced four of them and they all belong on adjacent
tracks in the same session.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu,
    QPushButton, QVBoxLayout, QWidget,
)

from . import dragout
from .widgets import FIELD_BUTTON_WIDTH, FIELD_WIDTH, centred_row

PathRole = Qt.UserRole + 1

#: What counts as something you would drag into a session. Anything else in
#: the folder is somebody else's file and is not listed.
AUDIO_SUFFIXES = frozenset({
    ".wav", ".aif", ".aiff", ".flac", ".alac", ".m4a", ".mp3", ".mp4",
    ".ogg", ".opus", ".wv", ".ape",
})

#: Ceiling on how much of a folder is listed. A sample library pointed at by
#: mistake should not lock the window up building ten thousand rows.
FOLDER_LIMIT = 300


def _size_label(path: Path) -> str:
    try:
        mb = path.stat().st_size / 1e6
    except OSError:
        return "missing"
    return f"{mb:.1f} MB" if mb >= 1 else f"{mb * 1000:.0f} KB"


class TrayList(QListWidget):
    """A list whose rows can be dragged out to Finder or Ableton."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragOnly)
        self.setDefaultDropAction(Qt.CopyAction)
        self.setAlternatingRowColors(True)
        self.setUniformItemSizes(True)

    def selected_paths(self) -> list[Path]:
        return [item.data(PathRole) for item in self.selectedItems()]

    def all_paths(self) -> list[Path]:
        return [self.item(i).data(PathRole) for i in range(self.count())]

    def startDrag(self, supported_actions: Qt.DropActions) -> None:
        """Hand over real file:// URIs rather than Qt's internal model data.

        QListWidget's default drag serialises model indexes, which Finder and
        Live cannot read. Overriding is what makes the drop land as files.
        """
        paths = self.selected_paths()
        if not paths:
            return
        label = Path(paths[0]).name if len(paths) == 1 else f"{len(paths)} files"
        dragout.start_drag(self, paths, label=label)

    def mimeData(self, items) -> object:
        """Also correct for any path that builds the payload without startDrag
        — kept in sync so there is one definition of the flavour."""
        return dragout.build_mime([i.data(PathRole) for i in items])


class DragTray(QWidget):
    """Downloaded files, newest first. Group 2's whole page."""

    #: Asked for from the context menu. The tray does not own a player — the
    #: window decides whether that means the selected file or the selected
    #: search result.
    preview_requested = Signal()
    #: Separating a file that is already here needs no download, so it lives
    #: in the context menu rather than as a button: Download is where a
    #: separation is normally asked for.
    separate_requested = Signal()
    #: The folder button. The tray does not own the setting, so it asks.
    folder_change_requested = Signal()
    #: Delete these files. The tray does not delete anything itself — the
    #: window owns what happens to a file and what is said about it.
    delete_requested = Signal(object)  # list[Path]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.list = TrayList(self)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._context_menu)

        # The same shape the search page opens with: a wide field over a
        # narrower button, both on the centre line. There it is what you are
        # looking for; here it is where what you found ended up.
        self.path_field = QLineEdit()
        self.path_field.setReadOnly(True)
        self.path_field.setPlaceholderText("No folder yet")
        self.path_field.setMaximumWidth(FIELD_WIDTH)
        self.path_field.setMinimumWidth(280)
        self.path_field.setCursorPosition(0)
        self.folder_button = QPushButton("Change…")
        self.folder_button.setFixedWidth(FIELD_BUTTON_WIDTH)
        self.folder_button.setAutoDefault(False)
        self.folder_button.setToolTip(
            "Save downloads somewhere else, and list what is already there."
        )
        self.folder_button.clicked.connect(self.folder_change_requested)

        self._hint = QLabel("Drag straight into Ableton or Finder.")
        self._hint.setStyleSheet("color: palette(mid);")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 0)
        self.path_row = centred_row(self.path_field, weight=2)
        self.button_row = centred_row(self.folder_button)
        layout.addLayout(self.path_row)
        layout.addLayout(self.button_row)
        layout.addWidget(self._hint)
        layout.addWidget(self.list, 1)

    # -- content ----------------------------------------------------------

    def add(self, path: Path | str, subtitle: str = "") -> QListWidgetItem | None:
        """Add a finished file. Returns None if it is not on disk.

        Refusing to list a missing file keeps the tray's promise: everything
        in it can be dragged.
        """
        path = Path(path)
        if not path.is_file():
            return None

        for existing in self.list.all_paths():
            if existing == path:
                return None  # already listed

        detail = subtitle or _size_label(path)
        item = QListWidgetItem(f"{path.name}\n{detail}")
        item.setData(PathRole, path)
        item.setToolTip(str(path))
        self.list.insertItem(0, item)
        self.list.setCurrentItem(item)
        return item

    def clear(self) -> None:
        """Empties the list only. The files stay where they were saved."""
        self.list.clear()

    def set_folder(self, path: Path | str) -> None:
        """Say where downloads are going. Display only — the setting lives in
        Settings, and this is the readout of it."""
        path = Path(path)
        self.path_field.setText(str(path))
        self.path_field.setToolTip(str(path))
        self.path_field.setCursorPosition(0)

    def show_folder(self, path: Path | str, limit: int = FOLDER_LIMIT) -> int:
        """List the audio already in a folder, newest first.

        The page is about the files you have, so it starts from what is on
        disk rather than from what this particular session happened to
        download. Returns how many were listed.
        """
        path = Path(path)
        self.set_folder(path)
        self.list.clear()
        try:
            files = [
                p for p in path.iterdir()
                if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES
            ]
        except OSError:
            return 0  # not there yet, or not readable: an empty list is honest
        files.sort(key=lambda p: p.stat().st_mtime)
        # Oldest first into a list that inserts at the top, so the newest
        # ends up on top and the cap drops the oldest.
        for p in files[-limit:]:
            self.add(p)
        return self.list.count()

    def count(self) -> int:
        return self.list.count()

    def paths(self) -> list[Path]:
        return self.list.all_paths()

    # -- actions ----------------------------------------------------------

    def reveal_selected(self) -> None:
        import subprocess

        paths = self.list.selected_paths() or self.list.all_paths()[:1]
        if paths:
            subprocess.run(["open", "-R", str(paths[0])], check=False)

    def build_menu(self) -> QMenu:
        """What right-clicking a file offers — which is everything the page
        can do to one.

        No button row under the list: this is a folder, and what you do to a
        file in a folder you do by right-clicking it. Built separately from
        showing it so the offer can be asserted without a modal menu.
        """
        menu = QMenu(self)
        menu.addAction("Preview", self.preview_requested.emit)
        menu.addAction("Separate…", self.separate_requested.emit)
        menu.addSeparator()
        menu.addAction("Reveal in Finder", self.reveal_selected)
        menu.addAction("Copy path", self._copy_paths)
        menu.addSeparator()
        # A view of a folder, so this deletes the file. Named for where it
        # goes, because "delete" that is recoverable and "delete" that is not
        # are different promises.
        menu.addAction("Move to Trash", self._delete_selected)
        return menu

    def _context_menu(self, point: QPoint) -> None:
        if not self.list.selectedItems():
            return
        self.build_menu().exec(self.list.mapToGlobal(point))

    def _copy_paths(self) -> None:
        from PySide6.QtWidgets import QApplication

        paths = self.list.selected_paths()
        QApplication.clipboard().setText("\n".join(str(p) for p in paths))

    def _delete_selected(self) -> None:
        paths = self.list.selected_paths()
        if paths:
            self.delete_requested.emit(paths)

    def remove_paths(self, paths) -> None:
        """Drop rows whose files are gone. Called after a delete, so the list
        and the folder still agree."""
        gone = {Path(p) for p in paths}
        for row in reversed(range(self.list.count())):
            if self.list.item(row).data(PathRole) in gone:
                self.list.takeItem(row)
