"""The compact media player and download bar at the bottom of the window."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame, QGraphicsDropShadowEffect, QGridLayout, QHBoxLayout, QLabel,
    QProgressBar, QPushButton, QSizePolicy, QSlider, QStackedWidget, QStyle,
    QVBoxLayout, QWidget,
)

from ..providers.base import Embed, LocalFile, Preview
from .preview import PreviewPane
from .widgets import ElidingLabel

#: The bar's own progress read-out. Narrow on purpose — the activity strip is
#: where a job is watched in detail; this is the corner-of-the-eye version.
PLAYER_HEIGHT = 82
STREAM_COLOUR = "#239b56"
TRANSFER_COLOUR = "#2f6fd0"
IDLE_COLOUR = "#777777"
ERROR_COLOUR = "#b43b3b"


class DownloadBar(QFrame):
    """Play the selected result, ask for a download, and report which is which."""

    requested = Signal()
    preview_requested = Signal()
    preview_closed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # Rounded and shadowed, so it reads as something lying on the page
        # rather than another band sawn off the bottom of it.
        self.setObjectName("downloadBar")
        self.setStyleSheet(
            "#downloadBar {"
            " background-color: #181818;"
            " border: 1px solid palette(mid);"
            " border-radius: 9px; }"
            "#downloadBar QLabel { color: #f2f2f2; }"
            "#downloadBar QSlider::groove:horizontal {"
            " height: 4px; background: #535353; border-radius: 2px; }"
            "#downloadBar QSlider::sub-page:horizontal {"
            " background: #f2f2f2; border-radius: 2px; }"
            "#downloadBar QSlider::handle:horizontal {"
            " background: #f2f2f2; width: 12px; margin: -4px 0;"
            " border-radius: 6px; }"
        )
        self.setFixedHeight(PLAYER_HEIGHT)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(14)
        shadow.setOffset(0, 2)
        shadow.setColor(QColor(0, 0, 0, 90))
        self.setGraphicsEffect(shadow)

        # This will become the hidden browser transport. It is deliberately not
        # part of the visible layout; the user controls it through this toolbar.
        self.player = PreviewPane(self)
        self.player.setVisible(False)
        self.player.error.connect(self.show_error)
        self.player.position_changed.connect(self._on_position_changed)
        self.player.duration_changed.connect(self._on_duration_changed)
        self.player.playing_changed.connect(self._on_playing_changed)
        self._preview_state: str | None = None
        self._preview_kind: str | None = None

        self.artwork = QLabel("♪")
        self.artwork.setAlignment(Qt.AlignCenter)
        self.artwork.setFixedSize(54, 54)
        self.artwork.setStyleSheet(
            "background: #303030; color: #b3b3b3; border-radius: 4px;"
            "font-size: 24px;"
        )

        self.title = ElidingLabel("Select a track", mode=Qt.ElideRight)
        self.title.setStyleSheet("font-weight: 600; color: #ffffff;")
        self.artist = ElidingLabel("", mode=Qt.ElideRight)
        self.artist.setStyleSheet("color: #b3b3b3; font-size: 11px;")

        track_text = QWidget()
        track_text_layout = QVBoxLayout(track_text)
        track_text_layout.setContentsMargins(0, 0, 0, 0)
        track_text_layout.setSpacing(2)
        track_text_layout.addWidget(self.title)
        track_text_layout.addWidget(self.artist)

        track = QWidget()
        track_layout = QHBoxLayout(track)
        track_layout.setContentsMargins(0, 0, 0, 0)
        track_layout.setSpacing(9)
        track_layout.addWidget(self.artwork)
        track_layout.addWidget(track_text, 1)
        track.setMinimumWidth(260)
        self.track_widget = track

        self.preview_button = QPushButton()
        self.preview_button.setAccessibleName("Preview")
        self.preview_button.setToolTip("Preview")
        self.preview_button.setIcon(
            self.style().standardIcon(QStyle.SP_MediaPlay)
        )
        self.preview_button.setFixedSize(38, 38)
        self.preview_button.setStyleSheet(
            "QPushButton { background: #ffffff; border: none;"
            " border-radius: 19px; }"
            "QPushButton:hover { background: #eeeeee; }"
            "QPushButton:disabled { background: #686868; }"
        )
        self.preview_button.setEnabled(False)
        self.preview_button.clicked.connect(self._on_preview_clicked)

        self.elapsed = QLabel("0:00")
        self.elapsed.setStyleSheet("color: #b3b3b3; font-size: 10px;")
        self.timeline = QSlider(Qt.Horizontal)
        self.timeline.setRange(0, 0)
        self.timeline.setEnabled(False)
        self.timeline.setMinimumWidth(280)
        self.timeline.sliderMoved.connect(self._show_scrub_position)
        self.timeline.sliderReleased.connect(self._commit_seek)
        self.duration_label = QLabel("0:00")
        self.duration_label.setStyleSheet("color: #b3b3b3; font-size: 10px;")

        timeline_row = QHBoxLayout()
        timeline_row.setContentsMargins(0, 0, 0, 0)
        timeline_row.setSpacing(7)
        timeline_row.addWidget(self.elapsed)
        timeline_row.addWidget(self.timeline, 1)
        timeline_row.addWidget(self.duration_label)

        transport = QWidget()
        transport_layout = QVBoxLayout(transport)
        transport_layout.setContentsMargins(0, 0, 0, 0)
        transport_layout.setSpacing(3)
        transport_layout.addWidget(
            self.preview_button, 0, Qt.AlignHCenter | Qt.AlignVCenter
        )
        transport_layout.addLayout(timeline_row)
        self.transport_widget = transport
        self.transport_layout = transport_layout

        self.state = QLabel()
        self.state.setAlignment(Qt.AlignCenter)
        self.state.setMinimumWidth(92)
        self.state.setFixedHeight(24)
        self._set_state("READY", IDLE_COLOUR)

        self.button = QPushButton("Download…")
        self.button.setEnabled(False)
        self.button.clicked.connect(self.requested.emit)
        self.button.setMinimumWidth(105)

        self.volume_icon = QLabel()
        self.volume_icon.setPixmap(
            self.style().standardIcon(QStyle.SP_MediaVolume).pixmap(16, 16)
        )
        self.volume_icon.setToolTip("Volume")
        self.volume = QSlider(Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(80)
        self.volume.setFixedWidth(78)
        self.volume.valueChanged.connect(self.player.set_volume)

        # The selected source's quality note remains available as a tooltip
        # without competing with the transport controls.
        self.note = ElidingLabel("", mode=Qt.ElideRight)
        self.note.setVisible(False)

        self.destination = QLabel("")
        self.destination.setVisible(False)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(3)
        self.progress.setVisible(False)
        self.progress_label = ElidingLabel("", mode=Qt.ElideMiddle)
        self.progress_label.setVisible(False)

        working = QWidget()
        working_layout = QVBoxLayout(working)
        working_layout.setContentsMargins(0, 0, 0, 0)
        working_layout.setSpacing(0)
        working_layout.addWidget(self.progress_label)
        working_layout.addWidget(self.progress)

        self.left = QStackedWidget()
        self.left.addWidget(self.destination)
        self.left.addWidget(working)
        self.left.setVisible(False)

        actions = QWidget()
        actions_layout = QHBoxLayout(actions)
        actions_layout.setContentsMargins(0, 0, 0, 0)
        actions_layout.setSpacing(8)
        actions_layout.addWidget(self.state)
        actions_layout.addWidget(self.volume_icon)
        actions_layout.addWidget(self.volume)
        actions_layout.addWidget(self.button)
        self.actions_widget = actions
        self.actions_layout = actions_layout

        grid = QGridLayout(self)
        grid.setContentsMargins(10, 7, 10, 7)
        grid.setHorizontalSpacing(14)
        grid.addWidget(track, 0, 0)
        grid.addWidget(transport, 0, 1)
        grid.addWidget(actions, 0, 2)
        grid.addWidget(self.progress, 1, 0, 1, 3)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 2)
        grid.setColumnStretch(2, 1)
        grid.setRowStretch(0, 1)
        self.grid = grid

    # -- what it says -----------------------------------------------------

    def set_available(self, available: bool) -> None:
        """Whether there is something selected to download."""
        self.preview_button.setEnabled(available)
        self.button.setEnabled(available)
        self.note.setVisible(available)

    def set_selection(
        self,
        title: str,
        artist: str | None = None,
        duration: float | None = None,
    ) -> None:
        if not self.previewing:
            self.title.setText(title)
            self.artist.setText(artist or "")
            total_ms = max(0, int((duration or 0) * 1000))
            self.timeline.setRange(0, total_ms)
            self.timeline.setValue(0)
            self.timeline.setEnabled(total_ms > 0)
            self.elapsed.setText("0:00")
            self.duration_label.setText(_format_time_ms(total_ms))

    def set_preview_label(self, text: str) -> None:
        self.preview_button.setAccessibleName(text)
        self.preview_button.setToolTip(text)

    def set_note(self, text: str) -> None:
        self.note.setText(text)

    def set_destination(self, text: str, tooltip: str = "") -> None:
        self.destination.setText(text)
        self.destination.setToolTip(tooltip or text)

    # -- preview ----------------------------------------------------------

    def show_preview(
        self,
        preview: Preview,
        title: str,
        artist: str | None = None,
        duration: float | None = None,
    ) -> None:
        self.title.setText(title)
        self.artist.setText(artist or "")
        total_ms = max(0, int((duration or 0) * 1000))
        self.timeline.setRange(0, total_ms)
        self.timeline.setValue(0)
        self.timeline.setEnabled(total_ms > 0)
        self.elapsed.setText("0:00")
        self.duration_label.setText(_format_time_ms(total_ms))
        if isinstance(preview, Embed):
            self._preview_kind = "embed"
        elif isinstance(preview, LocalFile):
            self._preview_kind = "local"
        else:
            self._preview_kind = None
        self.player.show_preview(preview)
        self.preview_button.setEnabled(True)
        self.preview_button.setIcon(
            self.style().standardIcon(QStyle.SP_MediaPause)
        )
        if isinstance(preview, Embed):
            if preview.autoplay:
                self._preview_state = "STREAMING"
            else:
                self._preview_state = "CLICK PLAY TO STREAM"
        elif isinstance(preview, LocalFile):
            self._preview_state = "PLAYING LOCAL"
        else:
            self._preview_state = "PREVIEW"
        self._restore_state()
        self.updateGeometry()

    def show_preview_loading(self, title: str, message: str = "Fetching preview…") -> None:
        self.title.setText(title)
        self.player.show_message(message)
        self._preview_state = "FETCHING PREVIEW"
        self._restore_state()
        self.updateGeometry()

    def show_error(self, message: str) -> None:
        self.player.show_message(message)
        self._preview_state = "PREVIEW UNAVAILABLE"
        self.preview_button.setIcon(
            self.style().standardIcon(QStyle.SP_MediaPlay)
        )
        self._set_state(self._preview_state, ERROR_COLOUR)
        self.updateGeometry()

    def stop_preview(self) -> None:
        self.player.stop()
        self._preview_state = None
        self._preview_kind = None
        self.timeline.setValue(0)
        self.elapsed.setText("0:00")
        self.preview_button.setIcon(
            self.style().standardIcon(QStyle.SP_MediaPlay)
        )
        self._restore_state()
        self.updateGeometry()

    # -- what it shows ----------------------------------------------------

    def show_progress(self, label: str, fraction: float, message: str = "") -> None:
        self.progress_label.setText(f"{label} — {message}" if message else label)
        self.progress.setValue(int(max(0.0, min(fraction, 1.0)) * 1000))
        self.left.setCurrentIndex(1)
        self.progress.setVisible(True)
        state = "STREAMING + DOWNLOADING" if self._preview_state == "STREAMING" \
            else "DOWNLOADING"
        self._set_state(state, TRANSFER_COLOUR)

    def clear_progress(self) -> None:
        self.left.setCurrentIndex(0)
        self.progress.setVisible(False)
        self._restore_state()

    def _restore_state(self) -> None:
        state = self._preview_state or "READY"
        colour = STREAM_COLOUR if self._preview_state else IDLE_COLOUR
        if state == "FETCHING PREVIEW":
            colour = TRANSFER_COLOUR
        elif state == "PREVIEW UNAVAILABLE":
            colour = ERROR_COLOUR
        self._set_state(state, colour)

    def _set_state(self, text: str, colour: str) -> None:
        self.state.setText(text)
        self.state.setStyleSheet(
            "QLabel {"
            f" color: white; background: {colour};"
            " border-radius: 5px; padding: 3px 7px; font-weight: 600;"
            "}"
        )

    # -- local transport --------------------------------------------------

    def _on_preview_clicked(self) -> None:
        if self._preview_kind == "local":
            self.player.toggle_playback()
            return
        self.preview_requested.emit()

    def _on_position_changed(self, position_ms: int) -> None:
        if self._preview_kind != "local" or self.timeline.isSliderDown():
            return
        self.timeline.setValue(max(0, position_ms))
        self.elapsed.setText(_format_time_ms(position_ms))

    def _on_duration_changed(self, duration_ms: int) -> None:
        if self._preview_kind != "local" or duration_ms <= 0:
            return
        self.timeline.setRange(0, duration_ms)
        self.timeline.setEnabled(True)
        self.duration_label.setText(_format_time_ms(duration_ms))

    def _show_scrub_position(self, position_ms: int) -> None:
        self.elapsed.setText(_format_time_ms(position_ms))

    def _commit_seek(self) -> None:
        if self._preview_kind == "local":
            self.player.seek(self.timeline.value())

    def _on_playing_changed(self, playing: bool) -> None:
        if self._preview_kind != "local":
            return
        self._preview_state = "PLAYING LOCAL" if playing else "PAUSED"
        icon = QStyle.SP_MediaPause if playing else QStyle.SP_MediaPlay
        self.preview_button.setIcon(self.style().standardIcon(icon))
        self._restore_state()

    @property
    def working(self) -> bool:
        return self.left.currentIndex() == 1

    @property
    def previewing(self) -> bool:
        return self._preview_state is not None


def _format_time_ms(milliseconds: int) -> str:
    seconds = max(0, milliseconds) // 1000
    minutes, remainder = divmod(seconds, 60)
    return f"{minutes}:{remainder:02d}"
