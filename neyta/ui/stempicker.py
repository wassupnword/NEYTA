"""The stem picker: checkboxes, and an estimate that is not a guess.

Your last selection is pre-ticked (build plan 1). All eight presets in
uvr-local/uvr.py are exposed; nothing is hidden.

The time estimate beside the list comes from `Calibration` — what this machine
actually did on previous runs. Before a preset has ever been run here it says
so rather than inventing a number, and it says "at least" whenever any part of
the selection is still unmeasured.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QPushButton,
    QVBoxLayout, QWidget,
)

from .. import config
from ..core import stems as stems_core


class StemPicker(QGroupBox):
    """Checkbox list plus a live, calibrated estimate."""

    selection_changed = Signal(object)  # list[str]

    def __init__(
        self,
        calibration: stems_core.Calibration | None = None,
        parent=None,
        *,
        separator=None,
    ):
        super().__init__("Stems", parent)
        self.calibration = calibration or stems_core.Calibration()
        #: The engine these boxes are pointed at. None means the local one,
        #: which can do everything — the picker's original assumption.
        self.separator = separator
        self._audio_seconds: float | None = None
        self._boxes: dict[str, QCheckBox] = {}
        self._supported = (
            tuple(separator.supported_options()) if separator is not None
            else tuple(o.key for o in config.STEM_OPTIONS)
        )

        grid = QGridLayout()
        for i, option in enumerate(config.STEM_OPTIONS):
            box = QCheckBox(option.label)
            tip = option.note or ""
            missing = (
                stems_core.missing_models(option.preset) if option.preset else []
            )
            if missing:
                # A few hundred MB downloads before any audio is processed;
                # better said here than mistaken for a hang.
                tip = f"{tip}\nDownloads {len(missing)} model(s) on first use."
                box.setText(f"{option.label} ⤓")
            if option.key not in self._supported:
                # Off rather than absent: the option exists, this engine
                # cannot produce it, and saying which is more use than a
                # shorter list that looks like the whole truth.
                box.setEnabled(False)
                tip = (
                    f"{self._engine_label} cannot produce this. Switch the "
                    "stem engine back to UVR in Settings for it."
                )
            if tip:
                box.setToolTip(tip.strip())
            box.toggled.connect(self._on_toggled)
            self._boxes[option.key] = box
            grid.addWidget(box, i // 2, i % 2)

        self.estimate = QLabel("")
        self.estimate.setWordWrap(True)
        self.estimate.setStyleSheet("color: palette(mid);")

        self.separate_button = QPushButton("Separate")
        self.clear_button = QPushButton("None")
        self.clear_button.setFixedWidth(64)
        self.clear_button.clicked.connect(self.clear)

        actions = QHBoxLayout()
        actions.addWidget(self.separate_button)
        actions.addWidget(self.clear_button)
        actions.addWidget(self.estimate, 1)

        layout = QVBoxLayout(self)
        layout.addLayout(grid)
        layout.addLayout(actions)

        self.set_selection(list(config.DEFAULT_STEMS))

    # -- selection --------------------------------------------------------

    @property
    def _engine_label(self) -> str:
        return getattr(self.separator, "label", "This engine")

    def supported(self) -> tuple[str, ...]:
        return self._supported

    def selection(self) -> list[str]:
        """Ticked keys, in the order they appear in the picker."""
        return [k for k, box in self._boxes.items() if box.isChecked()]

    def set_selection(self, keys: list[str]) -> None:
        """Tick exactly these.

        Signals are blocked per box so a ten-box update is one announcement
        rather than ten, but it is still announced: a caller watching the
        selection cares that it changed, not how it was changed.
        """
        # An unsupported option is never ticked, even by a remembered
        # selection: a disabled box can still be checked programmatically, and
        # that is how a separation gets asked for something it cannot deliver.
        wanted = {k for k in keys if k in self._supported}
        for key, box in self._boxes.items():
            box.blockSignals(True)
            box.setChecked(key in wanted)
            box.blockSignals(False)
        self._refresh()
        self.selection_changed.emit(self.selection())

    def clear(self) -> None:
        self.set_selection([])

    def set_audio_seconds(self, seconds: float | None) -> None:
        self._audio_seconds = seconds
        self._refresh()

    # -- work -------------------------------------------------------------

    def steps(self) -> list[stems_core.SeparationStep]:
        return stems_core.plan_separation(self.selection())

    def runs_a_model(self) -> bool:
        return bool(self.steps())

    def _on_toggled(self, _checked: bool) -> None:
        self._refresh()
        self.selection_changed.emit(self.selection())

    def _refresh(self) -> None:
        steps = self.steps()
        selected = self.selection()

        if not selected:
            self.estimate.setText("nothing ticked")
            self.separate_button.setEnabled(False)
            return

        self.separate_button.setEnabled(True)
        if getattr(self.separator, "uploads_audio", False):
            # A measured local throughput says nothing about a queue on
            # someone else's machine. What is worth knowing about this engine
            # is that the track leaves, and that it is metered.
            minutes = (self._audio_seconds or 0.0) / 60.0
            cost = f" — about {minutes:.1f} min of your plan" if minutes else ""
            self.estimate.setText(
                f"uploaded to {self._engine_label} and separated there{cost}"
            )
            return

        text = self.calibration.describe(steps, self._audio_seconds or 0.0)

        # Say when two ticked boxes cost one model run, because otherwise the
        # estimate looks wrong for the number of things ticked.
        if len(steps) < len([k for k in selected
                             if config.stem_option(k).preset is not None]):
            text = f"{text} — one model run covers several of these"
        self.estimate.setText(text)


class StemSummary(QWidget):
    """One line describing what a finished separation produced."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.label = QLabel("")
        self.label.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.label)

    def show_result(self, delivered: dict) -> None:
        if not delivered:
            self.label.setText("")
            return
        names = ", ".join(sorted(delivered))
        self.label.setText(f"{len(delivered)} stem(s): {names}")
