"""The phrase panel: hit list, waveform, in/out handles.

Build plan 5.5 and 5.8. Two things this deliberately makes visible:

  * the accuracy badge, because a word-accurate hit lands on the syllable and
    a line-accurate one lands somewhere in a two-second caption. Both are
    useful; conflating them is not.
  * the panel's own reach — "searched the top 30 results for this phrase",
    never "searched YouTube".

Line-accurate hits open with the trim handles active, because a two-second
window is a starting point you nudge rather than an answer.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton,
    QSizePolicy, QSpinBox, QVBoxLayout, QWidget,
)

from ..core import convert
from ..core.phrase import Hit

HitRole = Qt.UserRole + 1

WORD_COLOUR = QColor(38, 138, 76)
LINE_COLOUR = QColor(181, 115, 10)


class WaveformView(QWidget):
    """A waveform with draggable in and out handles."""

    span_changed = Signal(float, float)

    HANDLE_PX = 7

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._peaks: list[float] = []
        self.duration = 0.0
        self.start = 0.0
        self.end = 0.0
        self._dragging: str | None = None
        self.setMinimumHeight(92)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(True)
        self.setCursor(Qt.ArrowCursor)

    # -- content ----------------------------------------------------------

    def load(self, path: Path, duration: float | None = None) -> None:
        try:
            self._peaks = convert.peaks(path, buckets=max(200, self.width()))
            self.duration = duration or (convert.probe(path).duration or 0.0)
        except convert.ConversionError:
            self._peaks = []
            self.duration = duration or 0.0
        self.start, self.end = 0.0, self.duration
        self.update()

    def clear(self) -> None:
        self._peaks = []
        self.duration = self.start = self.end = 0.0
        self.update()

    def set_span(self, start: float, end: float) -> None:
        if self.duration <= 0:
            return
        self.start = max(0.0, min(start, self.duration))
        self.end = max(self.start, min(end, self.duration))
        self.update()
        self.span_changed.emit(self.start, self.end)

    @property
    def span(self) -> tuple[float, float]:
        return self.start, self.end

    # -- geometry ---------------------------------------------------------

    def _x(self, seconds: float) -> float:
        if self.duration <= 0:
            return 0.0
        return seconds / self.duration * self.width()

    def _seconds(self, x: float) -> float:
        if self.width() <= 0:
            return 0.0
        return max(0.0, min(x / self.width() * self.duration, self.duration))

    # -- painting ---------------------------------------------------------

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect()

        painter.fillRect(rect, QColor(246, 246, 248))
        if not self._peaks:
            painter.setPen(QColor(150, 150, 150))
            painter.drawText(rect, Qt.AlignCenter, "no clip loaded")
            return

        middle = rect.height() / 2
        width = rect.width()
        count = len(self._peaks)

        # Outside the selection first, then the selection over it, so the kept
        # region reads as the foreground rather than as a highlight.
        for i, level in enumerate(self._peaks):
            x = i / count * width
            seconds = self._seconds(x)
            inside = self.start <= seconds <= self.end
            painter.setPen(QPen(QColor(60, 110, 190) if inside
                                else QColor(200, 202, 208), 1))
            half = level * (rect.height() / 2 - 4)
            painter.drawLine(QPointF(x, middle - half), QPointF(x, middle + half))

        x0, x1 = self._x(self.start), self._x(self.end)
        painter.fillRect(QRectF(0, 0, x0, rect.height()), QColor(255, 255, 255, 150))
        painter.fillRect(
            QRectF(x1, 0, width - x1, rect.height()), QColor(255, 255, 255, 150)
        )

        painter.setPen(QPen(QColor(40, 40, 40), 2))
        for x in (x0, x1):
            painter.drawLine(QPointF(x, 0), QPointF(x, rect.height()))
        painter.setBrush(QBrush(QColor(40, 40, 40)))
        for x in (x0, x1):
            painter.drawRect(QRectF(x - 3, 0, 6, 12))

        painter.setPen(QColor(70, 70, 70))
        painter.drawText(
            rect.adjusted(6, 0, -6, -4),
            int(Qt.AlignRight | Qt.AlignBottom),
            f"{self.end - self.start:.2f}s of {self.duration:.2f}s",
        )

    # -- interaction ------------------------------------------------------

    def _near(self, x: float) -> str | None:
        if self.duration <= 0:
            return None
        if abs(x - self._x(self.start)) <= self.HANDLE_PX:
            return "start"
        if abs(x - self._x(self.end)) <= self.HANDLE_PX:
            return "end"
        return None

    def mousePressEvent(self, event) -> None:
        self._dragging = self._near(event.position().x())
        if self._dragging is None and self.duration > 0:
            # Clicking outside a handle moves the nearer one, so a cut can be
            # placed in one gesture instead of grabbing a 7-pixel target.
            seconds = self._seconds(event.position().x())
            self._dragging = (
                "start" if abs(seconds - self.start) < abs(seconds - self.end)
                else "end"
            )
            self._move_handle(seconds)

    def mouseMoveEvent(self, event) -> None:
        x = event.position().x()
        if self._dragging:
            self._move_handle(self._seconds(x))
        else:
            self.setCursor(
                Qt.SplitHCursor if self._near(x) else Qt.ArrowCursor
            )

    def mouseReleaseEvent(self, _event) -> None:
        self._dragging = None

    def _move_handle(self, seconds: float) -> None:
        if self._dragging == "start":
            self.set_span(min(seconds, self.end - 0.05), self.end)
        elif self._dragging == "end":
            self.set_span(self.start, max(seconds, self.start + 0.05))


class PhrasePanel(QWidget):
    """Hit list plus the trim controls for the selected hit."""

    hit_selected = Signal(object)   # Hit
    grab_requested = Signal(object, float, float)  # Hit, start, end
    search_requested = Signal(str, int, bool)      # phrase, candidates, fuzzy

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        self.enabled = QCheckBox("Search phrases")
        self.enabled.setToolTip(
            "Reads the captions of the top search results and finds the exact "
            "moment your words are spoken."
        )
        self.candidates = QSpinBox()
        self.candidates.setRange(5, 100)
        self.candidates.setValue(30)
        self.candidates.setPrefix("top ")
        self.fuzzy = QCheckBox("near-misses")
        self.fuzzy.setChecked(True)
        self.fuzzy.setToolTip(
            "Also match when the recogniser misheard one word of the phrase."
        )

        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("color: palette(mid);")

        self.hits = QListWidget()
        self.hits.setAlternatingRowColors(True)
        self.hits.currentItemChanged.connect(self._on_hit_changed)

        self.waveform = WaveformView()
        self.auto_trim = QCheckBox("Auto-trim silence")
        self.auto_trim.setChecked(True)
        self.reset_button = QPushButton("Reset handles")
        self.reset_button.clicked.connect(self._reset_span)
        self.grab_button = QPushButton("Cut this")
        self.grab_button.clicked.connect(self._grab)
        self.grab_button.setEnabled(False)

        controls = QHBoxLayout()
        controls.addWidget(self.enabled)
        controls.addWidget(self.candidates)
        controls.addWidget(self.fuzzy)
        controls.addStretch(1)

        actions = QHBoxLayout()
        actions.addWidget(self.grab_button)
        actions.addWidget(self.auto_trim)
        actions.addWidget(self.reset_button)
        actions.addStretch(1)

        # The checkbox row is always visible — it is how phrase mode is
        # turned on. Everything it produces lives in `body`, which collapses
        # so an unused panel costs one line rather than half the window.
        self.body = QWidget()
        body_layout = QVBoxLayout(self.body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.addWidget(self.summary)
        body_layout.addWidget(self.hits, 1)
        body_layout.addWidget(self.waveform)
        body_layout.addLayout(actions)
        self.body.setVisible(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(controls)
        layout.addWidget(self.body, 1)

        self.enabled.toggled.connect(self.body.setVisible)

        self._current: Hit | None = None
        self._clip: Path | None = None
        #: The engine in force, set by the window from Settings. None until
        #: then, which reads as the built-in one — the default.
        self._engine = None
        self.set_search(None)

    # -- which engine ------------------------------------------------------

    def _placeholder(self) -> str:
        """What the panel says before anything has been searched. It describes
        the engine that would actually run, because the two do different
        things and promise different reach."""
        if self._engine is not None and self._engine.key != "builtin":
            name = self._engine.label.split(" —")[0]
            return (
                f"Tick “Search phrases”, type words, and {name}'s caption "
                "index says which videos say them, and when."
            )
        return (
            "Tick “Search phrases”, type words, and NEYTA reads the "
            "captions of the top results to find them."
        )

    def set_engine(self, option) -> None:
        """Point the panel at the engine Settings chose.

        The near-misses box belongs to the built-in matcher, which fuzzes a
        window of tokens against a transcript this app fetched. An index
        lookup has no transcript on this side to be approximately right
        about, so the box is disabled rather than left looking as though it
        still does something.
        """
        self._engine = option
        index = option is not None and option.key != "builtin"
        self.fuzzy.setEnabled(not index)
        self.fuzzy.setToolTip(
            "The index matches the phrase exactly; there is nothing here to "
            "fuzz it against."
            if index else
            "Also match when the recogniser misheard one word of the phrase."
        )
        self.enabled.setToolTip(
            "Looks the phrase up in a caption index and lands on the moment "
            "it is spoken."
            if index else
            "Reads the captions of the top search results and finds the exact "
            "moment your words are spoken."
        )
        if not self.hits.count():
            self.summary.setText(self._placeholder())

    # -- content ----------------------------------------------------------

    def set_search(self, search) -> None:
        self.hits.clear()
        self.waveform.clear()
        self._current = None
        self._clip = None
        self.grab_button.setEnabled(False)

        if search is None:
            self.summary.setText(self._placeholder())
            return

        self.summary.setText(search.summary)
        for hit in search.hits:
            item = QListWidgetItem(self._describe(hit))
            item.setData(HitRole, hit)
            item.setForeground(
                WORD_COLOUR if hit.accuracy == "word" else LINE_COLOUR
            )
            item.setToolTip(f"{hit.title}\n…{hit.context}…")
            self.hits.addItem(item)
        if search.hits:
            self.hits.setCurrentRow(0)

    @staticmethod
    def _describe(hit: Hit) -> str:
        score = "" if hit.score >= 0.999 else f"  ~{hit.score:.2f}"
        return (
            f"[{hit.badge}] {hit.label}{score}   {hit.matched}\n"
            f"    {hit.title[:70]}"
        )

    def load_clip(self, path: Path, hit: Hit | None = None) -> None:
        """Show a fetched clip so its edges can be nudged before keeping it."""
        self._clip = Path(path)
        self.waveform.load(self._clip)
        hit = hit or self._current
        if hit is not None and hit.needs_trimming and self.auto_trim.isChecked():
            self._apply_auto_trim()
        self.grab_button.setEnabled(True)

    def _apply_auto_trim(self) -> None:
        if self._clip is None:
            return
        span = convert.tighten(self._clip, min_silence=0.15, threshold_db=-38)
        if span is not None and span.duration > 0.05:
            self.waveform.set_span(span.start, span.end)

    # -- interaction ------------------------------------------------------

    def _on_hit_changed(self, current: QListWidgetItem | None, _previous) -> None:
        if current is None:
            self._current = None
            return
        hit = current.data(HitRole)
        self._current = hit
        self.grab_button.setEnabled(True)
        # A line-accurate hit opens with the handles live, because its window
        # is a couple of seconds wide and you will want to move them.
        self.reset_button.setEnabled(True)
        self.hit_selected.emit(hit)

    def _reset_span(self) -> None:
        self.waveform.set_span(0.0, self.waveform.duration)

    def _grab(self) -> None:
        if self._current is None:
            return
        if self._clip is not None and self.waveform.duration > 0:
            start, end = self.waveform.span
        else:
            start, end = self._current.padded()
        self.grab_requested.emit(self._current, start, end)

    @property
    def current_hit(self) -> Hit | None:
        return self._current
