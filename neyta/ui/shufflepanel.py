"""Crate-digging, on the YouTube tab.

samplette-local's library is 180,000 Discogs releases resolved to YouTube
videos, with genre, style, region, key and tempo attached. A shuffled track is
an ordinary YouTube result, so it drops into the same list, the same format
picker and the same drag tray as anything you searched for.

The panel hides itself when there is no library, rather than offering a button
that cannot work.
"""

from __future__ import annotations

import re

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox, QGridLayout, QLabel, QLineEdit, QSpinBox, QVBoxLayout, QWidget,
)

from ..core import samplette

ANY = "any"
_RANGE_TEXT = re.compile(
    r"^\s*(?:(?P<low>\d+)\s*)?(?:-\s*(?P<high>\d+)\s*)?$|^\s*(?P<exact>\d+)\s*$"
)


class _Facet(QComboBox):
    """A filter dropdown populated from what the library actually contains, so
    it can never offer a value that matches nothing."""

    def __init__(self, label: str, parent=None) -> None:
        super().__init__(parent)
        self.setEditable(False)
        self.addItem(f"{label}: {ANY}", None)

    def load(self, values: list[tuple[str, int]]) -> None:
        for value, count in values:
            self.addItem(f"{value}  ({count})", value)

    def selection(self) -> samplette.TagFilter:
        value = self.currentData()
        return samplette.TagFilter.of(value) if value else samplette.TagFilter()


class ShufflePanel(QWidget):
    """Crate-dig filters for the YouTube shuffle controls."""

    #: list[SampletteTrack]
    shuffled = Signal(object)
    message = Signal(str)
    state_changed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.library: samplette.SampletteLibrary | None = None
        self._matches = 0

        self.style = _Facet("style")
        self.genre = _Facet("genre")
        self.region = _Facet("region")
        self.key = _Facet("key")

        self.tempo = QLineEdit()
        self.tempo.setPlaceholderText("98")
        self.tempo.setToolTip("One BPM, for example 98.")

        self.year = QLineEdit()
        self.year.setPlaceholderText("1978")
        self.year.setToolTip("One year, for example 1978.")

        self.query = QLineEdit()
        self.query.setPlaceholderText("artist, title or release")

        self.count = QSpinBox()
        self.count.setRange(1, 50)
        self.count.setValue(10)
        self.count.setPrefix("draw ")

        self.mode = QComboBox()
        for label, value in (("shuffle", "shuffle"), ("most played", "popular"),
                             ("newest", "recent")):
            self.mode.addItem(label, value)

        self.summary = QLabel("")
        self.summary.setStyleSheet("color: palette(mid);")
        self.matches = QLabel("")
        self.matches.setStyleSheet("color: palette(mid);")

        grid = QGridLayout()
        for col, widget in enumerate((self.style, self.genre, self.region, self.key)):
            grid.addWidget(widget, 0, col)
        values = QVBoxLayout()
        values.setContentsMargins(0, 0, 0, 0)
        values.setSpacing(4)
        values.addWidget(QLabel("bpm"))
        values.addWidget(self.tempo)
        values.addWidget(QLabel("year"))
        values.addWidget(self.year)
        grid.addLayout(values, 1, 0, 1, 4)
        grid.addWidget(self.query, 2, 0, 1, 2)
        grid.addWidget(self.mode, 2, 2)
        grid.addWidget(self.count, 2, 3)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.summary)
        layout.addLayout(grid)
        layout.addWidget(self.matches)

        for widget in (self.style, self.genre, self.region, self.key, self.mode):
            widget.currentIndexChanged.connect(self._update_count)
        for widget in (self.tempo, self.year, self.query):
            widget.textChanged.connect(self._update_count)

    # -- library ----------------------------------------------------------

    def attach(self, library: samplette.SampletteLibrary | None) -> None:
        """Wire up a library, or hide if there is not one."""
        self.library = library
        self._matches = 0
        if library is None:
            self.summary.setText("")
            self.matches.setText("")
            self.state_changed.emit()
            return
        self.style.load(library.facet("styles", 60))
        self.genre.load(library.facet("genres", 40))
        self.region.load(library.facet("regions", 40))
        self.key.load(library.facet("keys", 30))

        low, high = library.bounds("tempo")
        if low is not None and high is not None:
            self.tempo.setPlaceholderText(str(int((low + high) / 2)))
        low, high = library.bounds("year")
        if low is not None and high is not None:
            self.year.setPlaceholderText(str(int((low + high) / 2)))
        stats = library.stats()
        self.summary.setText(
            f"Crate dig — {stats.ready:,} playable of {stats.total:,} crawled"
        )
        self._update_count()

    # -- filters ----------------------------------------------------------

    @staticmethod
    def _parse_range(text: str, *, label: str) -> tuple[samplette.Range, str | None]:
        example = "1978 or 1970-1979" if label == "year" else "98 or 90-110"
        raw = text.strip().replace("–", "-").replace("—", "-")
        if not raw:
            return samplette.Range(), None
        if "-" not in raw:
            if raw.isdigit():
                value = int(raw)
                return samplette.Range(value, value), None
            return samplette.Range(), f"{label} must look like {example}"
        match = _RANGE_TEXT.fullmatch(raw)
        if not match:
            return samplette.Range(), f"{label} must look like {example}"
        low = match.group("low")
        high = match.group("high")
        if low is None and high is None:
            return samplette.Range(), f"{label} must look like {example}"
        low_value = int(low) if low is not None else None
        high_value = int(high) if high is not None else None
        if (
            low_value is not None and high_value is not None
            and low_value > high_value
        ):
            return samplette.Range(), f"{label} range is backwards"
        return samplette.Range(low_value, high_value), None

    def filters(self) -> samplette.Filters:
        tempo, tempo_error = self._parse_range(self.tempo.text(), label="tempo")
        if tempo_error is not None:
            raise ValueError(tempo_error)
        year, year_error = self._parse_range(self.year.text(), label="year")
        if year_error is not None:
            raise ValueError(year_error)

        return samplette.Filters(
            query=self.query.text().strip(),
            styles=self.style.selection(),
            genres=self.genre.selection(),
            regions=self.region.selection(),
            keys=self.key.selection(),
            tempo=tempo,
            year=year,
        )

    def _update_count(self) -> None:
        if self.library is None:
            self._matches = 0
            self.matches.setText("")
            self.state_changed.emit()
            return
        try:
            n = self.library.count(self.filters())
        except ValueError as exc:
            self._matches = 0
            self.matches.setText(str(exc))
            self.state_changed.emit()
            return
        self._matches = n
        self.matches.setText(f"{n:,} match" if n else "nothing matches")
        self.state_changed.emit()

    def can_shuffle(self) -> bool:
        return self.library is not None and self._matches > 0

    def shuffle(self) -> None:
        if self.library is None:
            return
        tracks = self.library.sample(
            self.count.value(), self.filters(), mode=self.mode.currentData()
        )
        if not tracks:
            self.message.emit("Nothing matches those filters.")
            return
        self.shuffled.emit(tracks)
