"""Small widgets shared between panels.

One place for the pieces that more than one panel needs, so a label that has
to shrink rather than shove behaves the same everywhere it appears.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QWidget

#: The centred field-and-button pair both pages open with: a wide thing to
#: read or type into, and a narrower button under it. Shared so the two pages
#: are the same shape rather than two shapes that happen to look alike.
FIELD_WIDTH = 620
FIELD_BUTTON_WIDTH = 150


def centred_row(widget: QWidget, weight: int = 0) -> QHBoxLayout:
    """One widget on the centre line, with equal air either side.

    `weight` lets the widget take a share of the row — the search field wants
    to grow toward its maximum on a wide window, a fixed-width button does
    not.
    """
    row = QHBoxLayout()
    row.addStretch(1)
    row.addWidget(widget, weight)
    row.addStretch(1)
    return row


class ElidingLabel(QLabel):
    """A label that shrinks its text instead of the row it is in.

    Track titles are long and the strip is one line tall, so something has to
    give. Eliding in the middle keeps both the format prefix and the end of
    the title, which is where the version ("- live", "- remaster") lives.
    """

    def __init__(self, text: str = "", mode=Qt.ElideMiddle, parent=None) -> None:
        super().__init__(parent)
        self._full = ""
        self._mode = mode
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802 — Qt's spelling
        self._full = text or ""
        self.setToolTip(self._full)
        super().setText(self._elided())

    def full_text(self) -> str:
        return self._full

    def _elided(self) -> str:
        return self.fontMetrics().elidedText(
            self._full, self._mode, max(self.width(), 40)
        )

    def resizeEvent(self, event) -> None:
        super().setText(self._elided())
        super().resizeEvent(event)
