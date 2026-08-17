"""The preview pane hosted by the bottom player bar.

YouTube, SoundCloud and Bandcamp all preview through their official embedded
players in QtWebEngine, so nothing is downloaded to audition a track and the
artist still gets the play. Soulseek has no stream to scrub and arrives as
fetch-then-play, which is why the pane accepts a LocalFile as well as an
Embed.

QtWebEngine is still imported lazily: selecting a result does not build a
browser. Double-clicking or pressing Preview builds it once and expands this
pane inside the bar.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl, Qt, Signal
from PySide6.QtWidgets import (
    QLabel, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget,
)

from ..providers.base import Embed, LocalFile, Preview

PLACEHOLDER = "Select a result to preview it.\nNothing is downloaded to listen."


class PreviewPane(QWidget):
    """Hosts an embedded player, or says why there is not one."""

    error = Signal(str)
    position_changed = Signal(int)
    duration_changed = Signal(int)
    playing_changed = Signal(bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._view = None
        self._player = None
        self._audio = None
        self._volume = 0.8
        self._is_playing = False

        self._placeholder = QLabel(PLACEHOLDER)
        self._placeholder.setAlignment(Qt.AlignCenter)
        self._placeholder.setWordWrap(True)
        self._placeholder.setStyleSheet("color: palette(mid);")

        self._stack = QStackedWidget(self)
        self._stack.addWidget(self._placeholder)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._stack)
        self.setMinimumHeight(200)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

    # -- web view ---------------------------------------------------------

    def _ensure_view(self):
        """Build the QWebEngineView on first use."""
        if self._view is not None:
            return self._view
        try:
            from PySide6.QtWebEngineCore import (
                QWebEnginePage, QWebEngineProfile, QWebEngineSettings,
            )
            from PySide6.QtWebEngineWidgets import QWebEngineView
        except ImportError as exc:
            self.error.emit(f"QtWebEngine unavailable: {exc}")
            return None

        # Off-the-record: the preview pane has no reason to accumulate
        # cookies or a cache for three third-party players.
        self._profile = QWebEngineProfile(self)
        self._view = QWebEngineView(self)

        page = QWebEnginePage(self._profile, self._view)
        page.settings().setAttribute(
            QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False
        )
        page.setAudioMuted(False)
        self._view.setPage(page)
        self._view.setContextMenuPolicy(Qt.NoContextMenu)
        self._view.loadFinished.connect(self._on_load_finished)
        self._stack.addWidget(self._view)
        return self._view

    def _on_load_finished(self, loaded: bool) -> None:
        if not loaded:
            self.error.emit("The preview player could not be loaded.")

    # -- api --------------------------------------------------------------

    def show_preview(self, preview: Preview) -> None:
        if isinstance(preview, Embed):
            self.show_embed(preview)
        elif isinstance(preview, LocalFile):
            self.show_local(preview.path)
        else:
            self.show_message("Nothing to preview.")

    def show_embed(self, embed: Embed) -> None:
        view = self._ensure_view()
        if view is None:
            self.show_message(
                "The embedded player needs QtWebEngine, which is not "
                "available. Downloads still work."
            )
            return
        view.setUrl(QUrl(embed.url))
        self._stack.setCurrentWidget(view)

    def show_local(self, path: Path) -> None:
        """A file already on disk — Soulseek's fetch-then-play, and a way to
        audition something that has just finished downloading."""
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

        if self._player is None:
            self._audio = QAudioOutput(self)
            self._audio.setMuted(False)
            self._audio.setVolume(self._volume)
            self._player = QMediaPlayer(self)
            self._player.setAudioOutput(self._audio)
            self._player.positionChanged.connect(self.position_changed.emit)
            self._player.durationChanged.connect(self.duration_changed.emit)
            self._player.playbackStateChanged.connect(
                lambda state: self._set_playing(
                    state == QMediaPlayer.PlaybackState.PlayingState
                )
            )
            self._player.errorOccurred.connect(
                lambda _error, message: self.error.emit(
                    message or "The local preview could not be played."
                )
            )
        self._player.setSource(QUrl.fromLocalFile(str(path)))
        self._player.play()
        self.show_message(f"Playing {Path(path).name}")

    def toggle_playback(self) -> None:
        if self._player is None:
            return
        if self._is_playing:
            self._player.pause()
        else:
            self._player.play()

    def seek(self, position_ms: int) -> None:
        if self._player is not None:
            self._player.setPosition(max(0, int(position_ms)))

    def set_volume(self, percent: int) -> None:
        self._volume = max(0.0, min(float(percent) / 100.0, 1.0))
        if self._audio is not None:
            self._audio.setVolume(self._volume)

    def _set_playing(self, playing: bool) -> None:
        self._is_playing = playing
        self.playing_changed.emit(playing)

    def show_message(self, text: str) -> None:
        self._placeholder.setText(text)
        self._stack.setCurrentWidget(self._placeholder)

    def stop(self) -> None:
        """Silence everything. Called when the tab changes or the window
        closes, so a player does not keep talking to an invisible pane."""
        if self._player is not None:
            self._player.stop()
        if self._view is not None:
            self._view.setUrl(QUrl("about:blank"))
        self.show_message(PLACEHOLDER)
