"""The result list — one model and one delegate for every tab.

Build plan 3.1: the result list is written once and never learns which service
it is pointed at. It renders a Result, and Result is the same shape whether it
came from ytsearch, scsearch, Bandcamp's autocomplete or a Soulseek peer.

The bitrate column is the honest one. It shows what the source actually has,
which for Bandcamp and Soulseek may be a lossless badge rather than a number,
and a dash when nothing is known until the item is probed.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Sequence

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

from ..providers.base import Media, Result

COL_TITLE = 0
COL_DURATION = 1
COL_QUALITY = 2
COLUMNS = ("Track", "Length", "Source")

#: Set on the quality column so the delegate can colour the badge without
#: re-deriving it from the Result.
QualityRole = Qt.UserRole + 1
ResultRole = Qt.UserRole + 2

_LOSSLESS_BADGES = {"FLAC", "WAV", "AIFF", "ALAC", "APE", "WV"}


def format_duration(seconds: float | None) -> str:
    if not seconds:
        return "—"
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


class ResultModel(QAbstractTableModel):
    """Rows of Result, with probe results merged in as they arrive.

    Search gives a cheap row; probing one item later fills in its true bitrate
    and duration. `apply_media` updates a row in place so the list does not
    reorder or flicker under the user's cursor.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows: list[Result] = []
        self._media: dict[int, Media] = {}

    # -- Qt ---------------------------------------------------------------

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole) -> Any:
        if not index.isValid():
            return None
        result = self._rows[index.row()]
        column = index.column()

        if role == ResultRole:
            return result
        if role == QualityRole:
            return self.quality_label(index.row())

        if role == Qt.DisplayRole:
            if column == COL_TITLE:
                return f"{result.artist} — {result.title}" if result.artist \
                    else result.title
            if column == COL_DURATION:
                return format_duration(result.duration)
            if column == COL_QUALITY:
                return self.quality_label(index.row())

        if role == Qt.TextAlignmentRole and column in (COL_DURATION, COL_QUALITY):
            return int(Qt.AlignRight | Qt.AlignVCenter)

        if role == Qt.ToolTipRole:
            media = self._media.get(index.row())
            if media is None:
                return f"{result.title}\n{result.url or ''}"
            streams = "\n".join(
                f"  {s.id}  {s.ext}  "
                f"{'lossless' if s.lossless else (f'{s.bitrate_kbps:.0f}k' if s.bitrate_kbps else '—')}"
                for s in media.streams if not s.has_video
            )
            return f"{result.title}\n{result.url or ''}\n\nstreams:\n{streams}"
        return None

    # -- content ----------------------------------------------------------

    def set_results(self, results: Sequence[Result]) -> None:
        self.beginResetModel()
        self._rows = list(results)
        self._media.clear()
        self.endResetModel()

    def clear(self) -> None:
        self.set_results([])

    def result_at(self, row: int) -> Result | None:
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def media_at(self, row: int) -> Media | None:
        return self._media.get(row)

    def apply_media(self, row: int, media: Media) -> None:
        """Merge a probe result into an existing row, in place."""
        if not 0 <= row < len(self._rows):
            return
        self._media[row] = media
        # Keep the row's own identity; take the richer metadata.
        self._rows[row] = replace(
            self._rows[row],
            title=media.result.title or self._rows[row].title,
            artist=media.result.artist or self._rows[row].artist,
            duration=media.result.duration or self._rows[row].duration,
            source_kbps=media.source_kbps,
        )
        left = self.index(row, 0)
        right = self.index(row, len(COLUMNS) - 1)
        self.dataChanged.emit(left, right)

    def quality_label(self, row: int) -> str:
        """What goes in the Source column.

        A probed row shows the truth. An unprobed row shows what search told
        us, which is usually nothing — and a dash is the honest rendering of
        "not known yet", not a placeholder for zero.
        """
        media = self._media.get(row)
        if media is not None:
            return media.quality_label
        result = self._rows[row]
        return f"{result.source_kbps:.0f}k" if result.source_kbps else "—"

    def is_lossless(self, row: int) -> bool:
        media = self._media.get(row)
        return bool(media and media.lossless)


class ResultDelegate(QStyledItemDelegate):
    """Draws the Source column as a badge.

    Green for a lossless source, plain for a bitrate, dim for unknown — so the
    tab that can actually give you a master is visible at a glance rather than
    buried in a tooltip.
    """

    LOSSLESS = QColor(38, 138, 76)
    KNOWN = QColor(80, 80, 80)
    UNKNOWN = QColor(150, 150, 150)

    def sizeHint(self, option, index) -> QSize:
        size = super().sizeHint(option, index)
        return QSize(size.width(), max(size.height(), 26))

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        if index.column() != COL_QUALITY:
            super().paint(painter, option, index)
            return

        label = index.data(QualityRole) or "—"
        self.initStyleOption(option, index)
        style = option.widget.style() if option.widget else None
        if style is not None:
            option.text = ""
            style.drawControl(QStyle.CE_ItemViewItem, option, painter, option.widget)

        upper = str(label).upper()
        if upper in _LOSSLESS_BADGES:
            background, foreground = self.LOSSLESS, QColor(Qt.white)
        elif label == "—":
            background, foreground = None, self.UNKNOWN
        else:
            background, foreground = None, self.KNOWN

        painter.save()
        font = QFont(option.font)
        font.setPointSizeF(max(9.0, option.font.pointSizeF() - 1))
        if background is not None:
            font.setBold(True)
        painter.setFont(font)

        metrics = painter.fontMetrics()
        text_width = metrics.horizontalAdvance(str(label))
        rect = option.rect.adjusted(0, 0, -8, 0)

        if background is not None:
            pad_x, height = 7, metrics.height() + 4
            badge_x = rect.right() - text_width - pad_x * 2
            badge_y = rect.center().y() - height // 2
            painter.setPen(Qt.NoPen)
            painter.setBrush(background)
            painter.drawRoundedRect(
                badge_x, badge_y, text_width + pad_x * 2, height, 4, 4
            )

        painter.setPen(foreground)
        painter.drawText(rect, int(Qt.AlignRight | Qt.AlignVCenter), str(label))
        painter.restore()
