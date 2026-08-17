"""The tab bar, painted in each service's own colour.

The colour is the tab's highlight, not its lettering: a bar of coloured words
is harder to read than a bar of plain ones, and the thing worth colouring is
which tab you are on. Selected tabs take the full colour with ink chosen for
contrast against it; the rest take a wash of the same colour, enough to tell
them apart at a glance without turning the row into a rainbow.

Qt has no per-tab background in its API, so the bar paints itself. Tabs with
no colour set fall back to the platform style untouched.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPalette
from PySide6.QtWidgets import QStyle, QStyleOptionTab, QStylePainter, QTabBar

#: How much of the colour an unselected tab keeps, out of 255.
WASH_ALPHA = 42
#: Corner radius of a tab's highlight.
RADIUS = 6


def ink_for(colour: QColor) -> QColor:
    """Black or white lettering, whichever the colour can carry.

    Perceived brightness rather than raw value: a saturated green is far
    lighter to the eye than a blue with the same numbers in it.
    """
    luma = 0.299 * colour.red() + 0.587 * colour.green() + 0.114 * colour.blue()
    return QColor("#141414") if luma > 150 else QColor("#ffffff")


class ColourTabBar(QTabBar):
    """A tab bar whose tabs can each be given a highlight colour."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._colours: dict[int, QColor] = {}
        self._active = True
        self.setExpanding(False)
        self.setDrawBase(False)

    def set_tab_colour(self, index: int, colour: str | QColor) -> None:
        self._colours[index] = QColor(colour)
        self.update()

    def tab_colour(self, index: int) -> QColor | None:
        return self._colours.get(index)

    def set_active(self, active: bool) -> None:
        """Whether this bar holds the current selection at all.

        A bar with one tab in it is always on that tab, so it has no
        unselected state of its own to show. Where two bars share a window,
        each has to be told when the other one has the floor.
        """
        if self._active != active:
            self._active = active
            self.update()

    def is_active(self) -> bool:
        return self._active

    def paintEvent(self, event) -> None:  # noqa: N802 — Qt's spelling
        painter = QStylePainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        for index in range(self.count()):
            colour = self._colours.get(index)
            if colour is None:
                option = QStyleOptionTab()
                self.initStyleOption(option, index)
                painter.drawControl(QStyle.CE_TabBarTab, option)
                continue

            selected = self._active and index == self.currentIndex()
            fill = QColor(colour)
            if not selected:
                fill.setAlpha(WASH_ALPHA)

            rect = self.tabRect(index)
            path = QPainterPath()
            path.addRoundedRect(
                QRectF(rect).adjusted(1.0, 2.0, -1.0, -1.0), RADIUS, RADIUS
            )
            painter.fillPath(path, fill)

            painter.setPen(
                ink_for(colour) if selected
                else self.palette().color(QPalette.WindowText)
            )
            painter.drawText(rect, Qt.AlignCenter, self.tabText(index))
