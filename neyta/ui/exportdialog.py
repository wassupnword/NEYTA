"""The export sheet — asked once, when you actually ask for a file.

Format and separation are per-file decisions, and none of them mean anything
until something is selected, so they do not belong on screen while you are
still searching. This dialog is what opens on Download, on Cut, and on
Separate; it closes having answered the only three questions that differ per
file: what to write, whether to run a separation first, and where it lands.

The source's own format is stated at the top and flagged again in the list,
because "MP3 320" is only a sensible answer once you know the original is a
129k AAC — the same honesty the format matrix already enforces (build plan
2.1), moved to the moment of choosing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel,
    QPushButton, QVBoxLayout,
)

from .. import config
from ..core import stems as stems_core
from ..providers.base import FormatOption, Media
from .stempicker import StemPicker

#: The verb on the primary button, per entry point.
MODE_VERB = {"download": "Download", "clip": "Cut", "separate": "Separate"}


# ---------------------------------------------------------------------------
# What the original already is
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceInfo:
    """The original file's type, as far as it is known right now.

    Deliberately allowed to be unknown: a result that has not been probed has
    no honest bitrate to show, and inventing the service's nominal ceiling
    here is exactly the guess the format matrix exists to avoid.
    """

    label: str = "not known yet"
    #: Container extension, lowercase and without the dot. Used to flag the
    #: export option that would keep the file as it already is.
    ext: str | None = None
    lossless: bool = False
    detail: str = ""

    @property
    def known(self) -> bool:
        return self.ext is not None or self.lossless

    @classmethod
    def unknown(cls, detail: str = "") -> "SourceInfo":
        return cls(detail=detail)

    @classmethod
    def from_media(cls, media: Media | None) -> "SourceInfo":
        best = media.best_audio if media else None
        if best is None:
            return cls.unknown("probe this result to see what it really is")
        ext = (best.ext or "").lower() or None
        codec = (best.codec or "").upper()
        parts = [ext.upper()] if ext else []
        if codec and codec.lower() != (ext or ""):
            parts.append(codec)
        if best.lossless:
            parts.append("lossless")
        elif best.bitrate_kbps:
            parts.append(f"{best.bitrate_kbps:.0f}k")
        if best.sample_rate:
            parts.append(f"{best.sample_rate / 1000:g} kHz")
        return cls(
            label=" · ".join(parts) or "unknown",
            ext=ext,
            lossless=best.lossless,
            detail=best.note,
        )

    @classmethod
    def from_file(cls, path: Path) -> "SourceInfo":
        """A file already on disk — the Separate entry point.

        ffmpeg is asked what is in it, but a missing or unreadable ffmpeg only
        costs the bitrate: the extension is still the truth about the
        container.
        """
        path = Path(path)
        ext = path.suffix.lstrip(".").lower() or None
        parts = [ext.upper()] if ext else []
        lossless = ext in ("wav", "aiff", "aif", "flac", "alac")
        try:
            from ..core import convert

            info = convert.probe(path)
        except Exception:  # noqa: BLE001 — an unreadable probe is not fatal
            info = None
        if info is not None:
            if info.codec and info.codec.lower() != (ext or ""):
                parts.append(info.codec.upper())
            if not lossless and info.bitrate_kbps:
                parts.append(f"{info.bitrate_kbps:.0f}k")
            if info.sample_rate:
                parts.append(f"{info.sample_rate / 1000:g} kHz")
        if lossless:
            parts.append("lossless")
        return cls(
            label=" · ".join(parts) or "unknown",
            ext=ext,
            lossless=lossless,
            detail=str(path),
        )


@dataclass(frozen=True)
class ExportChoice:
    """Everything the queue needs, decided in one place."""

    fmt: config.OutputFormat
    stems: list[str]
    dest_dir: Path

    @property
    def separates(self) -> bool:
        return bool(stems_core.plan_separation(self.stems))


def stem_format_options() -> list[FormatOption]:
    """The containers a separation can be written as.

    UVR emits WAV; everything else here is one ffmpeg pass over that WAV, so
    only what ffmpeg can encode to is offered. Nothing is marked as an upscale
    because a stem's bitrate is a property of the encode, not of the peer's
    file — the warning belongs on the download, which already carries it.
    """
    return [FormatOption(fmt) for fmt in config.STEM_FORMATS]


# ---------------------------------------------------------------------------
# The dialog
# ---------------------------------------------------------------------------


class ExportDialog(QDialog):
    """Format, separation and destination — asked once, then dismissed."""

    def __init__(
        self,
        *,
        settings,
        provider_key: str,
        format_options,
        source: SourceInfo | None = None,
        calibration: stems_core.Calibration | None = None,
        audio_seconds: float | None = None,
        #: The stem engine in play, or None for "assume the local one works".
        #: It carries its own availability, its own reason for not being
        #: available, and its own list of what it can produce.
        separator=None,
        mode: str = "download",
        subject: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.provider_key = provider_key
        self.mode = mode
        self.source = source or SourceInfo()
        self._options: list[FormatOption] = list(format_options)
        self._dest = Path(settings.download_dir)

        verb = MODE_VERB.get(mode, "Export")
        self.setWindowTitle(f"{verb} “{subject}”" if subject else verb)
        self.setModal(True)
        self.setMinimumWidth(520)

        # -- what it already is
        self.source_label = QLabel(f"Original: {self.source.label}")
        self.source_label.setStyleSheet("font-weight: 600;")
        self.source_label.setWordWrap(True)
        if self.source.detail:
            self.source_label.setToolTip(self.source.detail)

        # -- what to write
        self.format_box = QComboBox()
        self.format_note = QLabel("")
        self.format_note.setWordWrap(True)
        format_row = QHBoxLayout()
        format_row.addWidget(QLabel("Export as"))
        format_row.addWidget(self.format_box, 1)

        # -- whether to separate first
        self.stem_picker = StemPicker(calibration, separator=separator)
        self.stem_picker.setTitle("Separation")
        self.stem_picker.separate_button.setVisible(False)
        self.stem_picker.set_selection(list(settings.stem_selection))
        self.stem_picker.set_audio_seconds(audio_seconds)
        if separator is not None and not separator.available():
            self.stem_picker.setEnabled(False)
            self.stem_picker.setToolTip(separator.unavailable_note)

        # -- where it lands
        self.folder_label = QLabel("")
        self.folder_label.setStyleSheet("color: palette(mid);")
        self.folder_button = QPushButton("Change…")
        self.folder_button.setAutoDefault(False)
        folder_row = QHBoxLayout()
        folder_row.addWidget(QLabel("Save to"))
        folder_row.addWidget(self.folder_label, 1)
        folder_row.addWidget(self.folder_button)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, Qt.Horizontal, self
        )
        self.ok_button = self.buttons.button(QDialogButtonBox.Ok)
        self.ok_button.setText(verb)
        self.ok_button.setDefault(True)

        layout = QVBoxLayout(self)
        layout.addWidget(self.source_label)
        layout.addLayout(format_row)
        layout.addWidget(self.format_note)
        layout.addWidget(self.stem_picker)
        layout.addLayout(folder_row)
        layout.addWidget(self.buttons)

        self.format_box.currentIndexChanged.connect(self._on_format_changed)
        self.folder_button.clicked.connect(self.choose_folder)
        self.stem_picker.selection_changed.connect(lambda _keys: self._refresh_ok())
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        self._load_formats()
        self._update_folder_label()
        self._refresh_ok()

    # -- formats ----------------------------------------------------------

    def _remembered_format(self) -> str:
        if self.mode == "separate":
            return self.settings.get("format/stems", config.WAV_48_24.key)
        return self.settings.format_for(self.provider_key).key

    def _decorate(self, option: FormatOption) -> str:
        """The one line shown for a format, marks included.

        Two marks, and only two: this is what you already have, and this costs
        you something. Anything else read as decoration during the phase-7
        pass and made the honest warning easier to miss.
        """
        label = option.format.label
        if not option.available:
            return f"{label} — {option.note}"
        if self._keeps_original(option.format):
            label = f"{label} — same as the original"
        if option.note:
            label = f"{label}  ⚠"
        return label

    def _keeps_original(self, fmt: config.OutputFormat) -> bool:
        if fmt.kind == "source" and not fmt.ext:
            return True  # "Original file" — whatever the peer has
        if not fmt.ext or not self.source.ext:
            return False
        return fmt.ext.lower() == self.source.ext

    def _load_formats(self) -> None:
        self.format_box.blockSignals(True)
        self.format_box.clear()
        for option in self._options:
            self.format_box.addItem(self._decorate(option), option.format.key)
            index = self.format_box.count() - 1
            self.format_box.setItemData(index, option.note or "", Qt.ToolTipRole)
            if not option.available:
                self.format_box.model().item(index).setEnabled(False)
        chosen = self.format_box.findData(self._remembered_format())
        self.format_box.setCurrentIndex(max(chosen, 0))
        self.format_box.blockSignals(False)
        self._on_format_changed()

    def _on_format_changed(self) -> None:
        key = self.format_box.currentData()
        note = next(
            (o.note for o in self._options if o.format.key == key), None
        )
        self.format_note.setText(f"⚠ {note}" if note else "")
        self.format_note.setStyleSheet("color: #b5730a;" if note else "")

    # -- destination ------------------------------------------------------

    def choose_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Save to", str(self._dest)
        )
        if chosen:
            self._dest = Path(chosen)
            self._update_folder_label()

    def _update_folder_label(self) -> None:
        self.folder_label.setText(self._dest.name or str(self._dest))
        self.folder_label.setToolTip(str(self._dest))

    # -- result -----------------------------------------------------------

    def _refresh_ok(self) -> None:
        """Separate with nothing ticked would run nothing at all; every other
        entry point still has a file to write."""
        if self.mode != "separate":
            return
        self.ok_button.setEnabled(bool(
            stems_core.plan_separation(self.stem_picker.selection())
        ))

    def choice(self) -> ExportChoice:
        return ExportChoice(
            fmt=config.format_by_key(self.format_box.currentData()),
            stems=self.stem_picker.selection(),
            dest_dir=self._dest,
        )

    def accept(self) -> None:
        """Confirming is also what makes these the next defaults."""
        choice = self.choice()
        if self.mode == "separate":
            self.settings.set("format/stems", choice.fmt.key)
        else:
            self.settings.set_format_for(self.provider_key, choice.fmt)
        self.settings.stem_selection = choice.stems
        self.settings.download_dir = choice.dest_dir
        super().accept()
