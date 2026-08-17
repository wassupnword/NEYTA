"""The page bar: one icon per page, in the top-left corner.

Two pages, both on screen at once — a magnifier for looking for music, a
folder for the files you already have — with the one you are on filled in.
A switcher that showed only its destination had to be read before it could be
understood; a row of pages can be recognised without reading it at all.

The icons are drawn rather than typed. An emoji folder brings its own colours
with it, which fight the page colour behind it and ignore the ink chosen for
contrast against that colour; a stroked path takes the pen it is given, so the
same icon works filled, washed, on a light theme and on a dark one.

Colour follows the tab bar: the page you are on takes the full colour with ink
picked for contrast, the other takes a wash of its own colour.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPalette, QPen
from PySide6.QtWidgets import QAbstractButton, QHBoxLayout, QWidget

from .tabbar import RADIUS, WASH_ALPHA, ink_for

#: The icons this bar knows how to draw.
ICON_SEARCH = "search"
ICON_FOLDER = "folder"
ICON_GEAR = "gear"

#: A little more colour under the pointer than at rest, so an icon that is not
#: the page you are on still answers to being aimed at.
HOVER_ALPHA = 90

#: Same box as the gear in the opposite corner, so the top row reads as one
#: strip of controls rather than three sizes of button.
BUTTON_SIZE = QSize(46, 40)
#: The icon inside it. Short of the button's edges: the fill is the highlight,
#: and an icon that reaches the corners leaves it no margin to show in.
ICON_SIZE = 20
#: Icon stroke, in pixels. Matched to the weight of the gear glyph beside it.
STROKE = 1.8
#: How much of the magnifier's box the lens takes. The rest is handle.
LENS = 0.72
#: Teeth on the gear. Six rather than the usual eight: at twenty pixels across,
#: eight teeth close up into a ring of noise.
GEAR_TEETH = 6
#: The cog's line. Thinner than the other two so its teeth keep an inside.
GEAR_STROKE = 1.6

#: The unseen-arrivals dot, top-right of the folder.
BADGE_RADIUS = 8.0

PAGE_SEARCH = "search"
PAGE_DOWNLOADED = "downloaded"


def _draw_search(painter: QPainter, box: QRectF, ink: QColor) -> None:
    """A magnifier: lens and handle, on the box's diagonal."""
    painter.setPen(QPen(ink, STROKE, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    painter.setBrush(Qt.NoBrush)
    lens = QRectF(box.left(), box.top(), box.width() * LENS, box.height() * LENS)
    painter.drawEllipse(lens)
    # Out of the lens's lower-right shoulder — the point on the rim that the
    # box's own diagonal passes through — to the corner. Starting at the rim
    # rather than near it is what keeps the handle joined to the glass.
    shoulder = 0.5 * LENS * (1.0 + 2.0 ** -0.5)
    painter.drawLine(
        QPointF(box.left() + box.width() * shoulder,
                box.top() + box.height() * shoulder),
        box.bottomRight(),
    )


def _draw_folder(painter: QPainter, box: QRectF, ink: QColor) -> None:
    """A folder seen face on: body, and the tab standing above its top edge."""
    painter.setPen(QPen(ink, STROKE, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    painter.setBrush(Qt.NoBrush)
    tab_w = box.width() * 0.40
    tab_h = box.height() * 0.18
    top = box.top() + box.height() * 0.10   # the folder is wider than it is tall
    body_top = top + tab_h

    path = QPainterPath()
    path.moveTo(box.left(), box.bottom())
    path.lineTo(box.left(), top)
    path.lineTo(box.left() + tab_w, top)
    path.lineTo(box.left() + tab_w + tab_h, body_top)
    path.lineTo(box.right(), body_top)
    path.lineTo(box.right(), box.bottom())
    path.closeSubpath()
    painter.drawPath(path)


def _polar(centre: QPointF, radius: float, degrees: float) -> QPointF:
    angle = math.radians(degrees)
    return QPointF(centre.x() + radius * math.cos(angle),
                   centre.y() + radius * math.sin(angle))


def _draw_gear(painter: QPainter, box: QRectF, ink: QColor) -> None:
    """A cog: a ring of teeth around a hub.

    Wide, shallow teeth and a thinner line than the other two icons. A cog is
    the most detailed of the three, and at twenty pixels a tooth drawn to the
    usual proportions is narrower than the stroke that outlines it, which
    turns the rim into a row of blobs.
    """
    painter.setPen(QPen(ink, GEAR_STROKE, Qt.SolidLine, Qt.RoundCap,
                        Qt.RoundJoin))
    painter.setBrush(Qt.NoBrush)
    centre = box.center()
    # Half a stroke in from the edge, so the ink lands inside the box rather
    # than straddling it.
    outer = box.width() / 2 - GEAR_STROKE / 2
    root = outer * 0.70
    step = 360.0 / GEAR_TEETH

    path = QPainterPath()
    for tooth in range(GEAR_TEETH):
        middle = tooth * step
        # Up the flank, across the tip, down the far flank, then along the
        # root to the next tooth.
        for offset, radius in ((-0.32, root), (-0.22, outer),
                               (0.22, outer), (0.32, root)):
            point = _polar(centre, radius, middle + offset * step)
            if tooth == 0 and offset == -0.32:
                path.moveTo(point)
            else:
                path.lineTo(point)
    path.closeSubpath()
    painter.drawPath(path)
    painter.drawEllipse(centre, outer * 0.30, outer * 0.30)


#: Icon name -> the function that draws it into a box in a given ink.
ICONS = {ICON_SEARCH: _draw_search, ICON_FOLDER: _draw_folder,
         ICON_GEAR: _draw_gear}


class PageButton(QAbstractButton):
    """One page, as an icon, in that page's colour."""

    def __init__(self, icon: str, label: str, colour: str | QColor,
                 parent=None) -> None:
        super().__init__(parent)
        self._icon = icon
        self._label = label
        self._colour = QColor(colour)
        self._badge = 0
        self.setCheckable(True)
        self.setFixedSize(BUTTON_SIZE)
        self.setCursor(Qt.PointingHandCursor)
        # Reachable from the keyboard, but not a place Tab stops on the way to
        # the search field: the pages are chrome, not part of the search.
        self.setFocusPolicy(Qt.TabFocus)
        self._refresh_names()

    # -- state ------------------------------------------------------------

    def set_colour(self, colour: str | QColor) -> None:
        """The search page wears whichever service it would take you back to,
        so this changes under the button rather than being set once."""
        colour = QColor(colour)
        if colour != self._colour:
            self._colour = colour
            self.update()

    def colour(self) -> QColor:
        return QColor(self._colour)

    def set_badge(self, count: int) -> None:
        """How much has landed that you have not looked at yet."""
        count = max(int(count), 0)
        if count != self._badge:
            self._badge = count
            self._refresh_names()
            self.update()

    def badge(self) -> int:
        return self._badge

    def badge_text(self) -> str:
        """Two characters at most: past a handful the number stops being a
        count and starts being "several"."""
        return "9+" if self._badge > 9 else str(self._badge)

    def nextCheckState(self) -> None:  # noqa: N802 — Qt's spelling
        """Pressing an icon asks for a page; it does not light one.

        The window answers by setting the whole row's state, so what is filled
        in is the page that is actually on screen rather than the last icon
        that was pressed.
        """

    def _refresh_names(self) -> None:
        """The icon and the dot both say something; the name says both of them
        out loud, for the tooltip and for a screen reader."""
        text = (f"{self._label} ({self._badge} new)" if self._badge
                else self._label)
        self.setToolTip(text)
        self.setAccessibleName(text)

    # -- painting ---------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802 — Qt's spelling
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        fill = QColor(self._colour)
        if not self.isChecked():
            fill.setAlpha(HOVER_ALPHA if self.underMouse() else WASH_ALPHA)
        path = QPainterPath()
        path.addRoundedRect(
            QRectF(self.rect()).adjusted(1.0, 2.0, -1.0, -1.0), RADIUS, RADIUS
        )
        painter.fillPath(path, fill)
        if self.hasFocus():
            painter.strokePath(path, QPen(self._colour, 1.5))

        ink = (ink_for(self._colour) if self.isChecked()
               else self.palette().color(QPalette.WindowText))
        box = QRectF(0, 0, ICON_SIZE, ICON_SIZE)
        box.moveCenter(QRectF(self.rect()).center())
        ICONS[self._icon](painter, box, ink)

        if self._badge:
            self._paint_badge(painter)

    def _paint_badge(self, painter: QPainter) -> None:
        """A dot in the page's own colour, ringed in the window's background
        so it stays a separate thing when the button behind it is filled."""
        rect = QRectF(0, 0, BADGE_RADIUS * 2, BADGE_RADIUS * 2)
        rect.moveTopRight(QRectF(self.rect()).adjusted(0, 2.0, -1.0, 0).topRight())
        painter.setPen(QPen(self.palette().color(QPalette.Window), 1.5))
        painter.setBrush(self._colour)
        painter.drawEllipse(rect)

        font = self.font()
        font.setPointSizeF(max(font.pointSizeF() - 2.0, 7.0))
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(ink_for(self._colour))
        painter.drawText(rect, Qt.AlignCenter, self.badge_text())


class PageBar(QWidget):
    """The row of pages. Emits the key of the one that was pressed."""

    #: Pressed, not changed: pressing the page you are already on is a no-op
    #: rather than a way to end up on neither.
    selected = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.buttons: dict[str, PageButton] = {}
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addStretch(0)
        self._layout = layout

    def add_page(self, key: str, icon: str, label: str,
                 colour: str | QColor) -> PageButton:
        button = PageButton(icon, label, colour, parent=self)
        button.clicked.connect(self._on_button_clicked)
        self.buttons[key] = button
        self._layout.insertWidget(self._layout.count() - 1, button)
        return button

    def _on_button_clicked(self, _checked: bool) -> None:
        """The visible page control is a single toggle: a click flips it to the
        other side of the same search/download state."""
        current = self.current()
        if current == PAGE_SEARCH:
            self.selected.emit(PAGE_DOWNLOADED)
        elif current == PAGE_DOWNLOADED:
            self.selected.emit(PAGE_SEARCH)

    def set_current(self, key: str | None) -> None:
        for name, button in self.buttons.items():
            button.setChecked(name == key)
            button.setVisible(name == key)

    def current(self) -> str | None:
        return next(
            (key for key, button in self.buttons.items() if button.isChecked()),
            None,
        )

    def set_colour(self, key: str, colour: str | QColor) -> None:
        self.buttons[key].set_colour(colour)

    def set_badge(self, key: str, count: int) -> None:
        self.buttons[key].set_badge(count)
