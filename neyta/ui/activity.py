"""The activity strip: the queue and the log, in one place along the bottom.

Every slow thing in NEYTA is a job, and everything the app has to say about
one is a message. Splitting those into two panels meant the answer to "what
happened?" was always in the other one — a download's failure was a row here
and its reason a line there. They interleave in arrival order instead, so the
strip reads top to bottom as what the app did.

One line per entry, jobs included: the progress bar sits in the row beside
its title rather than under it. A two-line job row meant three downloads
filled a strip that is meant to be glanced at, and the bars — the only part
that changes while you wait — were the first thing pushed out of sight.

It is a framed box like the result table and the finished-files list, with no
heading over it, and it is exactly one row tall — fixed, not draggable. What
it shows is always the newest entry, so the strip is a single line reading
"here is what just happened", and the window never has to give it more room
than that. The detail it used to make room for now has better homes: a
running transfer has its own bar over the results, and the history is still
here to scroll back through even though only one line of it shows.

Nothing is cleared by hand: finished entries fall off the top once the list
is long enough that nobody is reading them, and a job that is still running
is never one of them.
"""

from __future__ import annotations

import time

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QProgressBar, QPushButton, QScrollArea, QVBoxLayout,
    QWidget,
)

from ..core.jobs import JobSnapshot, JobState
from .qtbridge import JobBridge
from .widgets import ElidingLabel

#: How tall the strip is, in rows. One, always: it is a read-out, not a panel.
DEFAULT_ROWS = 1

#: Entries kept before the oldest finished ones are dropped. Long enough that
#: a whole session's downloads are still there to scroll back through, short
#: enough that a week-long run does not accumulate widgets forever.
MAX_ENTRIES = 200

_STATE_TEXT = {
    JobState.PENDING: "queued",
    JobState.RUNNING: "running",
    JobState.SUCCEEDED: "done",
    JobState.FAILED: "failed",
    JobState.CANCELLED: "cancelled",
}
#: What "this finished, and it worked" looks like. Shared with the Downloaded
#: tab, which is where the things it finished end up.
SUCCESS_COLOUR = "#2a8a4a"

_STATE_COLOUR = {
    JobState.SUCCEEDED: SUCCESS_COLOUR,
    JobState.FAILED: "#b3261e",
    JobState.CANCELLED: "palette(mid)",
}
_LEVEL_COLOUR = {"info": "palette(mid)", "error": "#b3261e"}


class JobRow(QWidget):
    """One job, one line: what it is, how far along, and a way out."""

    cancel_requested = Signal(int)

    def __init__(self, snapshot: JobSnapshot, parent=None) -> None:
        super().__init__(parent)
        self.job_id = snapshot.id

        self.title = ElidingLabel(snapshot.label)
        self.title.setStyleSheet("font-weight: 600;")
        self.status = QLabel("")
        self.status.setStyleSheet("color: palette(mid);")

        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setFixedSize(140, 6)

        # A glyph rather than the word: at one line tall a labelled button
        # sets the height of every row in the strip.
        self.cancel = QPushButton("✕")
        self.cancel.setFlat(True)
        self.cancel.setFixedSize(18, 18)
        self.cancel.setToolTip("Cancel this job")
        self.cancel.setAccessibleName("Cancel")
        self.cancel.clicked.connect(lambda: self.cancel_requested.emit(self.job_id))

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 1, 6, 1)
        layout.setSpacing(8)
        layout.addWidget(self.title, 1)
        layout.addWidget(self.status)
        layout.addWidget(self.bar)
        layout.addWidget(self.cancel)

        self.update_from(snapshot)

    def update_from(self, snapshot: JobSnapshot) -> None:
        self.bar.setValue(int(snapshot.progress * 1000))

        text = _STATE_TEXT.get(snapshot.state, "")
        if snapshot.state is JobState.RUNNING and snapshot.message:
            text = snapshot.message
        elif snapshot.state is JobState.FAILED and snapshot.error is not None:
            # The reason, not just the word "failed" — a restricted stream and
            # a dead network need different responses from the user.
            text = f"failed — {type(snapshot.error).__name__}"
            self.setToolTip(str(snapshot.error))
        if snapshot.attempt > 1 and not snapshot.state.terminal:
            text = f"{text} (attempt {snapshot.attempt})"

        self.status.setText(text)
        colour = _STATE_COLOUR.get(snapshot.state)
        self.status.setStyleSheet(f"color: {colour or 'palette(mid)'};")

        # A finished job keeps its place in the log but stops taking up the
        # controls of a live one.
        self.cancel.setVisible(not snapshot.state.terminal)
        self.bar.setVisible(not snapshot.state.terminal)

    def set_stripe(self, colour: QColor) -> None:
        """Paint this row's band. The row has no stylesheet of its own, so
        the palette is enough and the children inherit it."""
        self.stripe = colour
        palette = self.palette()
        palette.setColor(QPalette.Window, colour)
        self.setPalette(palette)
        self.setAutoFillBackground(True)


class LogLine(ElidingLabel):
    """One thing the app said, stamped with when it said it."""

    def __init__(self, text: str, level: str = "info", parent=None) -> None:
        super().__init__(f"{time.strftime('%H:%M:%S')}  {text}",
                         mode=Qt.ElideRight, parent=parent)
        self.message = text
        self.setToolTip(text)
        self._colour = _LEVEL_COLOUR.get(level, "palette(mid)")
        self.setStyleSheet(f"color: {self._colour};")
        self.setContentsMargins(6, 0, 6, 0)

    def set_stripe(self, colour: QColor) -> None:
        """Both halves in one rule: a widget carrying a stylesheet stops
        honouring an autofilled palette background, so the band has to come
        from the same place the text colour does."""
        self.stripe = colour
        self.setStyleSheet(
            f"color: {self._colour}; background-color: {colour.name()};"
        )


class ActivityPanel(QWidget):
    """Live jobs and the running log, one scrolling list of one-line rows."""

    def __init__(self, bridge: JobBridge, parent=None) -> None:
        super().__init__(parent)
        self.bridge = bridge
        self._rows: dict[int, JobRow] = {}
        self._lines: list[LogLine] = []
        #: Every entry in the order it arrived — what trimming walks.
        self._entries: list[QWidget] = []

        self._empty = QLabel("Nothing running. Downloads and messages appear here.")
        self._empty.setStyleSheet("color: palette(mid);")
        self._empty.setContentsMargins(6, 0, 6, 0)
        self._empty.setFixedHeight(self.row_height())

        self._container = QWidget()
        self._container.setBackgroundRole(QPalette.Base)
        self._container.setAutoFillBackground(True)
        self._list = QVBoxLayout(self._container)
        self._list.setContentsMargins(0, 0, 0, 0)
        # No gap: the bands have to meet, or the stripe reads as a box
        # around each row rather than as a continuous list.
        self._list.setSpacing(0)
        self._list.addWidget(self._empty)
        self._list.addStretch(1)

        # Framed and banded like the result table and the finished-files
        # list. A heading over it would only name what its own contents
        # already say, and at three rows tall the label cost a third of the
        # panel.
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(self._container)
        self.scroll.setFrameShape(QScrollArea.StyledPanel)
        self.scroll.setBackgroundRole(QPalette.Base)
        self.scroll.viewport().setAutoFillBackground(True)
        # No bars on a one-line box: it always shows the newest entry, and a
        # scrollbar in a 39px strip is more chrome than content.
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 2, 8, 4)
        layout.setSpacing(0)
        layout.addWidget(self.scroll, 1)

        # Fixed: the strip is one line and stays one line, so nothing in the
        # window can be squeezed by it and it can never be dragged away.
        self.setFixedHeight(self.preferred_height())
        #: Set when an entry arrives, cleared once the view has caught up to
        #: it. Inserting a widget does not resize the container until the
        #: layout runs, so scrolling to the bottom in the same call scrolls
        #: to where the bottom *was*; the scrollbar's own range change is
        #: what says the new row exists.
        self._follow = False
        self.scroll.verticalScrollBar().rangeChanged.connect(self._on_range_changed)
        bridge.changed.connect(self._on_change)

    # -- sizing -----------------------------------------------------------

    def row_height(self) -> int:
        """One line of text, plus the padding around it."""
        return max(self.fontMetrics().height(), 18) + 5

    def preferred_height(self, rows: int = DEFAULT_ROWS) -> int:
        """Height of the box holding `rows` entries, frame and margins in.

        Exact rather than roomy: the viewport has to be a whole number of
        rows tall, or bottom-aligned content leaves a sliver of the entry
        above showing along the top edge.
        """
        return 8 + rows * self.row_height()

    def sizeHint(self) -> QSize:
        return QSize(super().sizeHint().width(), self.preferred_height())

    # -- content ----------------------------------------------------------

    def _append(self, widget: QWidget) -> None:
        """Add at the bottom, above the trailing stretch, and show it.

        The newest entry is always the one on screen: this is the strip you
        glance at to see what the app is doing now, and a download that
        started while you were scrolled up is exactly the thing you looked
        down for.
        """
        # Every entry the same height, so one row of viewport shows exactly
        # one entry rather than one and a slice of another.
        widget.setFixedHeight(self.row_height())
        self._list.insertWidget(self._list.count() - 1, widget)
        self._entries.append(widget)
        self._empty.setVisible(False)
        self._trim()
        self._restripe()
        self.scroll_to_latest()

    def scroll_to_latest(self) -> None:
        """Show the newest entry, now and again once the layout has caught
        up with the row that was just added."""
        self._follow = True
        bar = self.scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _on_range_changed(self, _minimum: int, maximum: int) -> None:
        if self._follow:
            self._follow = False
            self.scroll.verticalScrollBar().setValue(maximum)

    def stripe_colours(self) -> tuple[QColor, QColor]:
        """The two bands, taken from the palette rather than hard-coded, so
        they are the same pair the result table and the tray are banded with
        — black and grey in a dark theme, white and grey in a light one."""
        palette = self.palette()
        return palette.color(QPalette.Base), palette.color(QPalette.AlternateBase)

    def _restripe(self) -> None:
        """Re-band every entry from the top.

        Done over the whole list rather than per row on the way in: trimming
        removes entries from the top, and a row that keeps the colour it was
        given at birth leaves two of the same shade adjacent afterwards.
        """
        base, alternate = self.stripe_colours()
        for index, widget in enumerate(self._entries):
            widget.set_stripe(base if index % 2 == 0 else alternate)

    def changeEvent(self, event) -> None:
        """Follow the system theme. Switching to dark mode swaps the palette
        under a stripe that was calculated from the old one."""
        super().changeEvent(event)
        if event.type() == QEvent.PaletteChange:
            self._restripe()

    def _trim(self) -> None:
        """Drop the oldest finished entries once there are too many.

        Anything still running is skipped however old it is: the strip must
        never lose the thing you are waiting on.
        """
        for widget in list(self._entries):
            if len(self._entries) <= MAX_ENTRIES:
                return
            if isinstance(widget, JobRow):
                snapshot = self.bridge.queue.get(widget.job_id)
                if snapshot is not None and not snapshot.state.terminal:
                    continue
                self._rows.pop(widget.job_id, None)
            else:
                self._lines.remove(widget)
            self._entries.remove(widget)
            self._list.removeWidget(widget)
            widget.deleteLater()

    def log(self, text: str, level: str = "info") -> None:
        """Record something the app said. Blank messages are the status bar
        clearing itself, not an event."""
        text = (text or "").strip()
        if not text:
            return
        if self._lines and self._lines[-1].message == text:
            return  # the same message twice running says nothing new
        line = LogLine(text, level)
        self._lines.append(line)
        self._append(line)

    def _on_change(self, event: str, snapshot: JobSnapshot) -> None:
        row = self._rows.get(snapshot.id)
        if row is None:
            row = JobRow(snapshot)
            row.cancel_requested.connect(self.bridge.queue.cancel)
            self._rows[snapshot.id] = row
            self._append(row)
        else:
            row.update_from(snapshot)
