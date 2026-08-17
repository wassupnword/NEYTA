"""First run.

Deliberately not a wizard that stands between you and the app. Three of the
four tabs work with no setup at all — that was measured, not assumed — so
gating the whole program behind a credentials form would be asking for
something it does not need.

What this does instead: say plainly what works now, what needs a login, and
what is missing on this machine, with one button that opens Settings. It
appears once and then stops.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QLabel, QPushButton, QVBoxLayout,
    QWidget,
)

from .. import config
from ..core import samplette
from ..core import stems as stems_core


class Readiness:
    """What this machine can do right now, checked rather than assumed."""

    def __init__(self, settings, slskd=None, lucida=None) -> None:
        self.settings = settings
        self.slskd = slskd
        self.lucida = lucida

    def rows(self) -> list[tuple[str, bool, str]]:
        """(label, ready, detail) — ready meaning usable without further setup."""
        out: list[tuple[str, bool, str]] = []

        for key in ("youtube", "soundcloud", "bandcamp"):
            out.append((
                config.FORMATS and _label(key), True,
                "works now — no account needed",
            ))

        if self.slskd is not None:
            state = self.slskd.status()
            ready = state.installed and state.configured
            if not state.installed:
                detail = "needs slskd — NEYTA can fetch it from Settings"
            elif not state.configured:
                detail = "needs your slsknet login — add it in Settings"
            else:
                detail = "configured; start it from Settings"
            out.append(("Soulseek", ready, detail))

        if self.lucida is not None:
            state = self.lucida.status()
            out.append((
                "Spotify", state.installed,
                "lucida-flow is here" if state.installed
                else "needs lucida-flow beside NEYTA — see Settings",
            ))

        # The chosen engine, not the local one: a machine with no uvr-local
        # and a LALAL.AI key can separate perfectly well, and saying it cannot
        # would be wrong.
        separator = stems_core.separator_for(self.settings)
        ready = separator.available()
        if separator.key == "uvr":
            detail = ("8 UVR presets ready" if ready
                      else "uvr-local is not built — run tools/setup.sh")
        else:
            detail = (f"{separator.label}, in the cloud" if ready
                      else separator.unavailable_note)
        out.append((f"Stem separation ({separator.label})", ready, detail))

        has_library = samplette.SampletteLibrary.available()
        detail = "not built — run samplette-local once"
        if has_library:
            try:
                with samplette.SampletteLibrary() as library:
                    stats = library.stats()
                detail = f"{stats.ready:,} playable tracks"
            except samplette.SampletteUnavailable:
                has_library = False
        out.append(("Crate dig", has_library, detail))

        ffmpeg = config.find_ffmpeg()
        out.append((
            "ffmpeg", ffmpeg is not None,
            str(ffmpeg) if ffmpeg else "missing — run tools/setup.sh",
        ))
        return out


def _label(key: str) -> str:
    return {"youtube": "YouTube", "soundcloud": "SoundCloud",
            "bandcamp": "Bandcamp"}[key]


class WelcomeDialog(QDialog):
    """Shown once, on the first run."""

    def __init__(self, settings, slskd=None, parent=None, lucida=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.open_settings = False
        self.setWindowTitle(f"Welcome to {config.APP_NAME}")
        self.setMinimumWidth(520)

        heading = QLabel(f"<h2>{config.APP_NAME}</h2>")
        intro = QLabel(
            "Search, preview, cut and stem-split audio from five sources, "
            "then drag the result straight into Ableton.<br><br>"
            "<b>Nothing needs setting up to start.</b> YouTube, SoundCloud "
            "and Bandcamp all work without an account."
        )
        intro.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addWidget(heading)
        layout.addWidget(intro)
        layout.addSpacing(8)
        layout.addWidget(_rule())

        for label, ready, detail in Readiness(settings, slskd, lucida).rows():
            layout.addWidget(_status_row(label, ready, detail))

        layout.addWidget(_rule())
        note = QLabel(
            "Soulseek is the only tab that needs an account, and the only one "
            "that can hand you a genuine master. Bandcamp comes close where "
            "the artist has enabled downloading."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: palette(mid);")
        layout.addWidget(note)

        self.show_again = QCheckBox("Show this again next time")
        layout.addWidget(self.show_again)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        settings_button = QPushButton("Open Settings…")
        settings_button.clicked.connect(self._open_settings)
        buttons.addButton(settings_button, QDialogButtonBox.ActionRole)
        buttons.rejected.connect(self.accept)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    def _open_settings(self) -> None:
        self.open_settings = True
        self.accept()

    def accept(self) -> None:
        # Recorded whichever way the dialog is dismissed, so a stray close
        # does not mean it reappears forever.
        self.settings.onboarding_complete = not self.show_again.isChecked()
        super().accept()

    @staticmethod
    def should_show(settings) -> bool:
        return not settings.onboarding_complete


def _status_row(label: str, ready: bool, detail: str) -> QWidget:
    row = QLabel(
        f"{'✓' if ready else '○'}  <b>{label}</b> — "
        f"<span style='color: palette(mid);'>{detail}</span>"
    )
    row.setTextFormat(Qt.RichText)
    row.setWordWrap(True)
    row.setStyleSheet(f"color: {'#2a8a4a' if ready else '#8a6a2a'};")
    return row


def _rule() -> QWidget:
    from PySide6.QtWidgets import QFrame

    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    line.setFrameShadow(QFrame.Sunken)
    return line
