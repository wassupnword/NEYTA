"""The main window: two groups, one search bar, one activity strip.

Build plan 3.1 in practice — the source bar swaps a Provider and nothing else
changes. The search bar, result list, export dialog, activity strip and drag
tray are built once here and never learn which service they are pointed at.

Group 1 is the four sources, centred in the tab row; group 2 is the folder in
the page bar, top-left, which takes the whole window when you press it and
gives the sources back when you press the magnifier beside it.
"""

from __future__ import annotations

import logging
import sqlite3
import subprocess
from pathlib import Path

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QGridLayout, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPushButton, QStackedWidget,
    QStatusBar, QTableView, QVBoxLayout, QWidget,
)

from .. import config
from ..core import convert, naming, samplette, trash
from ..core import phrase as phrase_core
from ..core import stems as stems_core
from ..core.engine import EngineError
from ..core.jobs import JobState
from ..vendor.lucida_bootstrap import LucidaBootstrap
from ..vendor.slskd_bootstrap import SlskdBootstrap
from ..providers.bandcamp import BandcampProvider
from ..providers.base import Embed, LocalFile, Media, NotSupported, Provider, Result
from ..providers.lucida import LucidaProvider
from ..providers.soulseek import SoulseekProvider, SoulseekUnavailable
from ..providers.soundcloud import SoundCloudProvider
from ..providers.youtube import YouTubeProvider
from . import results as results_ui
from .activity import SUCCESS_COLOUR, ActivityPanel
from .downloadbar import DownloadBar
from .exportdialog import ExportChoice, ExportDialog, SourceInfo, stem_format_options
from .onboarding import WelcomeDialog
from .pagebar import ICON_FOLDER, ICON_GEAR, ICON_SEARCH, PageBar, PageButton
from .phrasepanel import PhrasePanel
from .qtbridge import JobBridge
from .settingspage import SettingsDialog
from .shufflepanel import ShufflePanel
from .tabbar import ColourTabBar
from .tray import DragTray
from .widgets import (
    FIELD_BUTTON_WIDTH, FIELD_WIDTH, ElidingLabel, centred_row,
)

log = logging.getLogger(__name__)

#: Group 1: where music is found. One tab per service, and swapping between
#: them swaps a Provider and nothing else (build plan 3.1).
TABS: tuple[tuple[str, type[Provider]], ...] = (
    ("youtube", YouTubeProvider),
    ("soundcloud", SoundCloudProvider),
    ("bandcamp", BandcampProvider),
    ("soulseek", SoulseekProvider),
    ("spotify", LucidaProvider),
)

#: The two pages: source tabs and downloaded files. Settings uses the gear and
#: opens in its own modeless dialog.
GROUP_SOURCES = 1
GROUP_DOWNLOADED = 2

#: What each page is called, where it has to be said in words: the tooltip on
#: its icon, and what a screen reader reads out.
SEARCH_LABEL = "Search"
DOWNLOADED_LABEL = "Downloaded"
SETTINGS_LABEL = "Settings"

#: The keys the page icons are known by.
PAGE_SEARCH = "search"
PAGE_DOWNLOADED = "downloaded"
#: Which group each icon stands for, and the way back.
PAGE_GROUPS = {
    PAGE_SEARCH: GROUP_SOURCES,
    PAGE_DOWNLOADED: GROUP_DOWNLOADED,
}
GROUP_PAGES = {group: page for page, group in PAGE_GROUPS.items()}

#: Each source tab in its own service's colour, so the bar says where you are
#: before you have read a word of it. Mid-tone rather than the exact brand
#: hex where that would be illegible: these have to hold up as small text on
#: both a light and a dark background.
TAB_COLOURS: dict[str, str] = {
    "youtube": "#e62117",     # YouTube red
    "soundcloud": "#ff5500",  # SoundCloud orange
    "bandcamp": "#6e7378",    # Bandcamp grey
    "soulseek": "#2f6fd0",    # Soulseek blue
    # Spotify green, darkened from #1db954: the brand green on a light theme
    # is a highlight you cannot read black or white text against.
    "spotify": "#12a150",
}
#: Downloaded is not a service. Green because it is the same green the
#: activity strip marks a finished job with, and this is where those land.
DOWNLOADED_COLOUR = SUCCESS_COLOUR
#: Settings is not a service either, and it is not where work finishes. Slate:
#: the one page in the app that is about the app rather than about music.
SETTINGS_COLOUR = "#5d6480"

#: Where a search is slow enough that the wait needs explaining. The default
#: is "Searching X…", which is right for the tabs that answer in a second.
SEARCH_NOTE: dict[str, str] = {
    "soulseek": "Asking the Soulseek network — this takes 15-30 seconds…",
    "spotify": "Asking lucida — the first search also starts its browser…",
}




class MainWindow(QMainWindow):
    """Everything above the provider layer."""

    def __init__(self, services, parent=None) -> None:
        super().__init__(parent)
        self.services = services
        self.settings = services.settings
        self.bridge = JobBridge(services.queue, self)

        self.slskd = SlskdBootstrap(
            services.paths, port=int(self.settings.get("soulseek/slskd_port", 5030))
        )
        #: Neither daemon is started here. Both come up the first time their
        #: own tab needs them, so opening the app starts nothing.
        self.lucida = LucidaBootstrap(
            port=int(self.settings.get("spotify/port", config.LUCIDA_PORT))
        )
        self.providers: dict[str, Provider] = self._build_providers()
        self._probe_jobs: dict[int, int] = {}  # job id -> row
        self._preview_job: int | None = None
        self._preview_result: Result | None = None
        #: download job -> (what was downloaded, what the export dialog said
        #: to do with it). The choice is captured at the dialog, not read back
        #: later, so changing it for the next file cannot retroactively change
        #: what a queued download is about to become.
        self._pending_stems: dict[int, tuple[Result, ExportChoice]] = {}

        self.setWindowTitle(config.APP_NAME)
        self.resize(1180, 780)
        self._build()
        self._connect()
        self._restore()

    # -- construction -----------------------------------------------------

    def _build_providers(self) -> dict[str, Provider]:
        """One provider per tab. Two of them do not take a yt-dlp engine: both
        talk to a local daemon of their own instead."""
        daemons = {"soulseek": self.slskd, "spotify": self.lucida}
        return {
            key: (cls(bootstrap=daemons[key]) if key in daemons
                  else cls(self._engine()))
            for key, cls in TABS
        }

    def _engine(self):
        from ..core.engine import Engine

        cookie = self.settings.credential("youtube", "cookie_file")
        return Engine(
            cache=self.services.cache,
            cookie_file=Path(cookie) if cookie and self.settings.get(
                "youtube/use_cookies") else None,
        )

    def _build(self) -> None:
        self.tabs = ColourTabBar()
        for index, (key, _) in enumerate(TABS):
            self.tabs.addTab(self.providers[key].label)
            self.tabs.set_tab_colour(index, TAB_COLOURS[key])

        # The pages, as a bar of icons in the top-left corner: a magnifier for
        # looking for music, a folder for the files you already have. Both are
        # on screen at once and the one you are on is filled in — a switcher
        # that showed only its destination had to be read to be understood,
        # and never said which of the two you were looking at.
        self.page_bar = PageBar()
        self.page_bar.add_page(
            PAGE_SEARCH, ICON_SEARCH, SEARCH_LABEL, TAB_COLOURS[TABS[0][0]]
        )
        self.page_bar.add_page(
            PAGE_DOWNLOADED, ICON_FOLDER, DOWNLOADED_LABEL, DOWNLOADED_COLOUR
        )
        self._group = GROUP_SOURCES
        #: Files that have landed since you last looked at the page. A
        #: notification, not an inventory: the folder's own count is a fact
        #: about your disk, and putting it on the tab would leave a permanent
        #: "(214)" that means nothing has happened.
        self._unseen = 0
        #: The group-1 tab to come back to. Group 2 is not a source, so the
        #: provider, its format list and the ceiling note stay pointed at
        #: whichever service you were last searching.
        self._source_index = 0
        #: The source the search page is currently set up for. None until the
        #: first tab change does that setup.
        self._active_source: str | None = None

        # Centred, and the field is the wide one: what you aim at is the box
        # you type in, and the button is the same gesture as pressing Return.
        self.search = QLineEdit()
        self.search.setClearButtonEnabled(True)
        self.search.setMaximumWidth(FIELD_WIDTH)
        self.search.setMinimumWidth(280)
        self.search_button = QPushButton("Go")
        self.search_button.setDefault(True)
        self.search_button.setFixedWidth(FIELD_BUTTON_WIDTH)
        self.shuffle_button = QPushButton("Shuffle")
        self.shuffle_button.setFixedWidth(FIELD_BUTTON_WIDTH)
        self.shuffle_button.setDefault(False)
        self.shuffle_button.setAutoDefault(False)
        self.shuffle_settings_button = QPushButton("Shuffle settings")
        self.shuffle_settings_button.setAutoDefault(False)
        self.shuffle_settings_button.setCursor(Qt.PointingHandCursor)
        self.shuffle_settings_button.setStyleSheet(
            "QPushButton { border: none; padding: 0; color: palette(link); "
            "text-decoration: underline; }"
        )

        self.shuffle_panel = ShufflePanel()
        self.shuffle_popup = QDialog(self, Qt.Popup)
        self.shuffle_popup.setWindowTitle("Shuffle settings")
        popup_layout = QVBoxLayout(self.shuffle_popup)
        popup_layout.setContentsMargins(12, 12, 12, 12)
        popup_layout.addWidget(self.shuffle_panel)
        self._samplette_process: subprocess.Popen | None = None
        self._shuffle_when_ready = False
        self._samplette_timer = QTimer(self)
        self._samplette_timer.setInterval(2000)
        self._samplette_timer.timeout.connect(self._refresh_samplette_library)

        self.model = results_ui.ResultModel(self)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setItemDelegate(results_ui.ResultDelegate(self.table))
        self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.setSelectionMode(QTableView.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(results_ui.COL_TITLE, QHeaderView.Stretch)
        header.setSectionResizeMode(
            results_ui.COL_DURATION, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(
            results_ui.COL_QUALITY, QHeaderView.ResizeToContents)

        # Format, separation and destination are asked for in the export
        # dialog, at the moment they are needed. Nothing here has to be
        # decided while you are still searching.
        #
        # The bar is part of the search page, not chrome above it: it belongs
        # to the result you have selected, and it is where that download is
        # then watched.
        self.download_bar = DownloadBar()
        self.preview_button = self.download_bar.preview_button
        self.download_button = self.download_bar.button
        self.ceiling = self.download_bar.note
        self.folder_label = self.download_bar.destination
        # Settings keeps the far corner because it configures the app rather
        # than navigating between the two main pages.
        self.settings_button = PageButton(
            ICON_GEAR, SETTINGS_LABEL, SETTINGS_COLOUR
        )
        self.calibration = stems_core.Calibration(
            path=self.services.paths.support / "calibration.json"
        )
        # Do not read paid-service credentials while opening the window.
        # The requested separator is resolved when separation is explicitly
        # started instead.
        self.separator = stems_core.StemSeparator(calibration=self.calibration)

        self.phrase_panel = PhrasePanel()

        # The media surface lives in the bottom bar but remains lazy: selecting
        # a result shows controls without constructing QtWebEngine.
        self.preview = self.download_bar.player
        self.activity = ActivityPanel(self.bridge)
        self.tray = DragTray()

        # -- layout
        # Three columns with equal weight either side, so the sources land on
        # the window's centre line rather than on whatever is left over once
        # the page bar and the gear have taken their share. A row of stretches
        # cannot do that: the two ends are different widths.
        self.header_row = header = QGridLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(self.page_bar, 0, 0, Qt.AlignLeft | Qt.AlignVCenter)
        header.addWidget(self.tabs, 0, 1, Qt.AlignHCenter | Qt.AlignVCenter)
        header.addWidget(self.settings_button, 0, 2,
                         Qt.AlignRight | Qt.AlignVCenter)
        header.setColumnStretch(0, 1)
        header.setColumnStretch(2, 1)

        # 1 : 2 : 1, so the field is comfortably wide on a small window and
        # stops growing at FIELD_WIDTH on a large one. The Downloaded page
        # opens with the same pair, built by the same helper.
        self.search_row = search_row = centred_row(self.search, weight=2)
        self.search_controls = QWidget()
        controls_layout = QVBoxLayout(self.search_controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(4)

        button_row = QHBoxLayout()
        button_row.setContentsMargins(0, 0, 0, 0)
        button_row.setSpacing(8)
        button_row.addStretch(1)
        button_row.addWidget(self.search_button)
        button_row.addWidget(self.shuffle_button)
        button_row.addStretch(1)
        controls_layout.addLayout(button_row)
        controls_layout.addLayout(centred_row(self.shuffle_settings_button))
        self.button_row = button_row = centred_row(self.search_controls)

        self.search_page = QWidget()
        left_layout = QVBoxLayout(self.search_page)
        left_layout.setContentsMargins(8, 8, 8, 0)
        left_layout.addLayout(search_row)
        left_layout.addLayout(button_row)
        # Stretch is handed to whichever of these is actually showing; a
        # collapsed panel claiming a share of the height leaves a dead gap
        # down the middle of the window.
        left_layout.addWidget(self.phrase_panel, 0)
        left_layout.addWidget(self.table, 1)
        self._left_layout = left_layout

        self.settings_dialog = SettingsDialog(
            self.settings, bootstrap=self.slskd, cache=self.services.cache,
            lucida=self.lucida, parent=self,
        )
        self.settings_page = self.settings_dialog.page

        # One page per group. A stack rather than a splitter: they are
        # different activities, not halves of one, and each wants the whole
        # width for its own list.
        self.pages = QStackedWidget()
        self.pages.addWidget(self.search_page)      # group 1
        self.pages.addWidget(self.tray)             # group 2
        #: Which widget each group shows.
        self.group_pages = {
            GROUP_SOURCES: self.search_page,
            GROUP_DOWNLOADED: self.tray,
        }

        #: The bar's own three-column grid — the same centring the header
        #: uses, so the button lands on the window's centre line.
        self.controls_row = self.download_bar.grid

        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 6, 8, 6)
        root.setSpacing(4)
        root.addLayout(header)
        root.addWidget(self.pages, 1)
        root.addWidget(self.download_bar)
        # One line along the bottom, fixed. It belongs to neither group — a
        # download queued in one is still running in the other — and it takes
        # no room from either.
        root.addWidget(self.activity)
        self.root_layout = root

        self.setCentralWidget(central)
        # Kept as the message bus, not as a second place to read: everything
        # showMessage says is relayed into the activity log, which is where
        # it stays long enough to be read.
        self.setStatusBar(QStatusBar())
        self.statusBar().setVisible(False)

    def _connect(self) -> None:
        self.tabs.currentChanged.connect(self._on_tab_changed)
        # tabBarClicked as well as currentChanged: clicking the source tab
        # you are already on is how you come back from group 2, and Qt emits
        # no change for a click that does not change the index.
        self.tabs.tabBarClicked.connect(self._on_tab_changed)
        self.page_bar.selected.connect(self.show_page)
        self.search.returnPressed.connect(self.run_search)
        self.search_button.clicked.connect(self.run_search)
        self.shuffle_button.clicked.connect(self._shuffle)
        self.shuffle_settings_button.clicked.connect(self._toggle_shuffle_popup)
        self.table.selectionModel().currentRowChanged.connect(self._on_row_selected)
        self.table.doubleClicked.connect(self.preview_result_at)
        self.settings_button.clicked.connect(self.open_settings)
        self.settings_page.saved.connect(self._on_settings_saved)
        self.shuffle_panel.shuffled.connect(self._on_shuffled)
        self.shuffle_panel.message.connect(self.statusBar().showMessage)
        self.shuffle_panel.state_changed.connect(self._refresh_shuffle_controls)
        self.phrase_panel.enabled.toggled.connect(self._on_phrase_toggled)
        self.phrase_panel.hit_selected.connect(self._on_hit_selected)
        self.phrase_panel.grab_requested.connect(self._on_grab_hit)
        self.tray.preview_requested.connect(self.preview_selected)
        self.tray.separate_requested.connect(self.separate_selected)
        self.tray.folder_change_requested.connect(self.choose_download_folder)
        self.tray.delete_requested.connect(self.delete_files)
        # One relay instead of a logging call beside every showMessage.
        self.statusBar().messageChanged.connect(self.activity.log)
        self.download_bar.preview_requested.connect(self.preview_current_result)
        self.download_bar.preview_closed.connect(self.stop_preview)
        self.download_bar.requested.connect(self.download_selected)
        self.bridge.changed.connect(self._on_job_progress)
        self.bridge.succeeded.connect(self._on_job_succeeded)
        self.bridge.failed.connect(self._on_job_failed)

    def _restore(self) -> None:
        last = self.settings.get("ui/last_tab", "youtube")
        index = next((i for i, (k, _) in enumerate(TABS) if k == last), 0)
        self.tabs.setCurrentIndex(index)
        self._on_tab_changed(index)
        self._update_folder_label()
        # Start from what is on disk: the page is about the files you have,
        # not only the ones this session happened to fetch.
        self.tray.show_folder(Path(self.settings.download_dir))

        try:
            library = samplette.SampletteLibrary() \
                if samplette.SampletteLibrary.available() else None
        except samplette.SampletteUnavailable:
            library = None
        self.shuffle_panel.attach(library)
        self._refresh_shuffle_controls()

    def maybe_welcome(self) -> None:
        """First run only. Shown after the window, not instead of it."""
        if not WelcomeDialog.should_show(self.settings):
            return
        dialog = WelcomeDialog(
            self.settings, self.slskd, parent=self, lucida=self.lucida
        )
        dialog.exec()
        if dialog.open_settings:
            self.open_settings()

    # -- tab state --------------------------------------------------------

    @property
    def provider(self) -> Provider:
        return self.providers[self.tab_key]

    @property
    def tab_key(self) -> str:
        """The source tab in play — group 2 borrows the last one."""
        return TABS[max(0, min(self._source_index, len(TABS) - 1))][0]

    @property
    def group(self) -> int:
        return self._group

    def select_group(self, group: int) -> None:
        """Show one group and fill in its icon.

        Switching pages tears nothing down: the provider, the results and the
        player are all left as they are, so coming back finds the search
        exactly where you left it.
        """
        self._group = group
        if group is not GROUP_SOURCES:
            self.shuffle_popup.hide()
        if group is GROUP_DOWNLOADED:
            self._unseen = 0  # looking at them is what marks them seen
        self.pages.setCurrentWidget(self.group_pages[group])
        # Nothing to choose between anywhere but the search page, so the
        # sources go away rather than sit there inert.
        self.tabs.setVisible(group is GROUP_SOURCES)
        self._refresh_pages()
        self._refresh_controls()

    def show_page(self, key: str) -> None:
        """An icon in the page bar was pressed. Pressing the page you are
        already on lands you back on it, which is the point of it being a
        place rather than a door."""
        self.select_group(PAGE_GROUPS[key])

    def _refresh_pages(self) -> None:
        """Fill in the page you are on, and keep the search icon in the colour
        of the service it is pointed at, so the corner and the tab row agree
        about which one that is.

        The gear lives in the other corner but opens a dialog, so it never
        participates in page selection.
        """
        page = GROUP_PAGES[self._group]
        self.page_bar.set_colour(PAGE_SEARCH, TAB_COLOURS[self.tab_key])
        self.page_bar.set_badge(PAGE_DOWNLOADED, self._unseen)
        self.page_bar.set_current(page)
        self.settings_button.setChecked(False)

    def _refresh_controls(self) -> None:
        """Show the bottom bar for a selected result, playback, or transfer."""
        selected_source = (
            self._group is GROUP_SOURCES and self.download_button.isEnabled()
        )
        self.download_bar.setVisible(
            selected_source
            or self.download_bar.previewing
            or self.download_bar.working
        )

    def set_download_available(self, available: bool) -> None:
        """A result is selected, or none is. The button and the note about
        what that source can give you appear and disappear together."""
        self.download_bar.set_available(available)
        self._refresh_controls()

    #: What the bar reports on. A separation is the second half of the same
    #: request, so it keeps the bar rather than emptying it.
    TRANSFER_KINDS = ("download", "stems")

    def _on_job_progress(self, _event: str, snapshot) -> None:
        """Mirror the transfer into the bar, in the corner of the eye.

        The activity strip is where a job is watched in detail; this is the
        one line that says the thing you just asked for is happening.
        """
        if snapshot.kind not in self.TRANSFER_KINDS:
            return
        if not snapshot.state.terminal:
            self.download_bar.show_progress(
                snapshot.label, snapshot.progress, snapshot.message
            )
        elif not self._transfer_running():
            self.download_bar.clear_progress()
        self._refresh_controls()

    def _transfer_running(self) -> bool:
        return any(
            job.kind in self.TRANSFER_KINDS and not job.state.terminal
            for job in self.services.queue.jobs()
        )

    def note_arrival(self, files: int = 1) -> None:
        """Something finished. Badge the folder unless it is already open.

        Reading the page is what clears it, so the number always answers
        "how much have I not seen yet".
        """
        if files <= 0 or self._group is GROUP_DOWNLOADED:
            return
        self._unseen += files
        self._refresh_pages()

    def _on_tab_changed(self, index: int) -> None:
        self.select_group(GROUP_SOURCES)
        self._source_index = index
        # After the index moves, so the magnifier is in the colour of the tab
        # you have just landed on rather than the one you left.
        self._refresh_pages()
        if self.tab_key == self._active_source:
            return  # back from group 2 onto the source you left
        self._active_source = self.tab_key

        self.model.clear()
        self.stop_preview()
        self.settings.set("ui/last_tab", self.tab_key)
        self.download_bar.set_preview_label(self.provider.preview_label)
        self.ceiling.setText(self.provider.ceiling_note)
        self._refresh_phrase_engine()
        self._refresh_shuffle_controls()
        # Phrase search reads YouTube captions; the other tabs have none.
        on_youtube = self.tab_key == "youtube"
        phrasing = on_youtube and self.phrase_panel.enabled.isChecked()
        self.phrase_panel.setVisible(on_youtube)
        self.table.setVisible(not phrasing)
        self._left_layout.setStretchFactor(self.table, 0 if phrasing else 1)
        self._left_layout.setStretchFactor(self.phrase_panel, 1 if phrasing else 0)
        self.set_download_available(False)

    # -- the export dialog ------------------------------------------------

    def ask_export(
        self,
        *,
        mode: str,
        subject: str,
        media: Media | None = None,
        source: SourceInfo | None = None,
        audio_seconds: float | None = None,
    ) -> ExportChoice | None:
        """Put the export question on screen and return the answer, or None.

        Every entry point that writes a file comes through here, so format,
        separation and destination are asked in one place and remembered in
        one place.
        """
        options = (
            stem_format_options() if mode == "separate"
            else self.provider.format_options(media)
        )
        dialog = ExportDialog(
            settings=self.settings,
            provider_key=self.tab_key,
            format_options=options,
            source=source if source is not None else SourceInfo.from_media(media),
            calibration=self.calibration,
            audio_seconds=audio_seconds,
            separator=self.separator,
            mode=mode,
            subject=subject,
            parent=self,
        )
        if not dialog.exec():
            return None
        self._update_folder_label()
        return dialog.choice()

    # -- searching --------------------------------------------------------

    def run_search(self) -> None:
        query = self.search.text().strip()
        if not query:
            return
        if self._phrase_mode():
            self.run_phrase_search(query)
            return
        provider = self.provider
        if self.tab_key == "soulseek" and not self._ensure_soulseek():
            return
        if self.tab_key == "spotify" and not self._ensure_lucida():
            return
        self.statusBar().showMessage(SEARCH_NOTE.get(
            self.tab_key, f"Searching {provider.label}…"
        ))
        self.model.clear()

        def work(ctx):
            ctx.progress(0.1, "searching")
            return provider.search(query, 40)

        job = self.services.queue.submit(
            work, kind="search", label=f"Search {provider.label}: {query}"
        )
        self._search_job = job

    def _phrase_mode(self) -> bool:
        return self.tab_key == "youtube" and self.phrase_panel.enabled.isChecked()

    def _on_phrase_toggled(self, checked: bool) -> None:
        """Phrase search replaces the result list with the hit list.

        They answer different questions — "which video" versus "where in it" —
        and showing both at once made it unclear which one the format picker
        and Download button were pointed at.
        """
        self.settings.set("phrase/enabled", bool(checked))
        self.table.setVisible(not checked)
        self._left_layout.setStretchFactor(self.table, 0 if checked else 1)
        self._left_layout.setStretchFactor(self.phrase_panel, 1 if checked else 0)
        self._refresh_placeholder()
        if not checked:
            self.phrase_panel.set_search(None)

    def _toggle_shuffle_popup(self) -> None:
        if not self._shuffle_controls_visible():
            return
        if self.shuffle_popup.isVisible():
            self.shuffle_popup.hide()
            return
        self.shuffle_popup.adjustSize()
        pos = self.shuffle_settings_button.mapToGlobal(
            QPoint(0, self.shuffle_settings_button.height() + 4)
        )
        self.shuffle_popup.move(pos)
        self.shuffle_popup.show()

    def _shuffle_controls_visible(self) -> bool:
        return self.tab_key == "youtube"

    def _refresh_shuffle_controls(self) -> None:
        visible = self._shuffle_controls_visible()
        building = self._samplette_timer.isActive()
        has_library = self.shuffle_panel.library is not None
        self.shuffle_button.setVisible(visible)
        self.shuffle_settings_button.setVisible(visible)
        self.shuffle_button.setText("Building…" if building else "Shuffle")
        self.shuffle_button.setEnabled(
            visible and not building
            and (self.shuffle_panel.can_shuffle() if has_library else True)
        )
        self.shuffle_settings_button.setEnabled(visible and has_library)
        self.shuffle_button.setToolTip(
            "" if has_library else
            "Build the local crate on first use, then shuffle YouTube tracks."
        )
        if not visible:
            self.shuffle_popup.hide()

    def _shuffle(self) -> None:
        if self.shuffle_panel.library is not None:
            self.shuffle_panel.shuffle()
            return
        self._shuffle_when_ready = True
        self._start_samplette_library()

    def _start_samplette_library(self) -> None:
        script = config.SAMPLETTE_ROOT / "run.sh"
        if not script.is_file():
            self.statusBar().showMessage(
                f"Shuffle needs samplette-local at {config.SAMPLETTE_ROOT}"
            )
            self._shuffle_when_ready = False
            return
        if (
            self._samplette_process is None
            or self._samplette_process.poll() is not None
        ):
            log_path = self.services.paths.logs / "samplette.log"
            with log_path.open("ab") as output:
                self._samplette_process = subprocess.Popen(
                    [str(script), "--no-open"],
                    cwd=config.SAMPLETTE_ROOT,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        self.statusBar().showMessage(
            "Building the Shuffle library — first tracks take about a minute…"
        )
        self._samplette_timer.start()
        self._refresh_shuffle_controls()

    def _refresh_samplette_library(self) -> None:
        if samplette.SampletteLibrary.available():
            library = None
            try:
                library = samplette.SampletteLibrary()
                if library.count() > 0:
                    self.shuffle_panel.attach(library)
                    self._samplette_timer.stop()
                    self._refresh_shuffle_controls()
                    self.statusBar().showMessage("Shuffle library is ready.")
                    if self._shuffle_when_ready:
                        self._shuffle_when_ready = False
                        self.shuffle_panel.shuffle()
                    return
            except (samplette.SampletteUnavailable, sqlite3.Error):
                log.exception("samplette library is not ready")
            if library is not None:
                library.close()

        process = self._samplette_process
        if process is not None and process.poll() is not None:
            self._samplette_timer.stop()
            self._shuffle_when_ready = False
            self._refresh_shuffle_controls()
            self.statusBar().showMessage(
                "Could not build the Shuffle library; see samplette.log."
            )

    def _refresh_phrase_engine(self) -> None:
        """Point the phrase panel and the search field at the chosen engine.

        Called from the same place the placeholder is refreshed, because they
        are one fact shown twice: which engine your words are about to go to.
        """
        self.phrase_panel.set_engine(self._selected_phrase_engine())
        self._refresh_placeholder()

    def _selected_phrase_engine(self) -> config.EngineOption:
        """The displayed choice, without touching its Keychain credential."""
        key = str(self.settings.get(
            "phrase/engine", config.DEFAULT_PHRASE_ENGINE
        ))
        try:
            return config.phrase_engine(key)
        except ValueError:
            return config.phrase_engine(config.DEFAULT_PHRASE_ENGINE)

    def _refresh_placeholder(self) -> None:
        """The field says which service it is pointed at, so the tab you are
        on is legible from the thing you are about to type into.

        In phrase mode it names the engine as well: reading the top results and
        looking a phrase up in an index are different searches, and which one
        you are about to run should not be something you have to remember.
        """
        if self.tab_key != "youtube":
            self.search.setPlaceholderText(f"Searching {self.provider.label}…")
            return
        option = self._selected_phrase_engine()
        if option.key == "builtin":
            self.search.setPlaceholderText("Searching YouTube by captions…")
            return
        if not self._phrase_mode():
            self.search.setPlaceholderText(f"Searching {self.provider.label}…")
            return
        self.search.setPlaceholderText(
            f"Searching {option.label.split(' —')[0]}'s caption index…"
        )

    def run_phrase_search(self, phrase: str) -> None:
        engine = self.providers["youtube"].engine
        option = self.settings.phrase_engine
        api_key = self.settings.engine_key(option)
        candidates = self.phrase_panel.candidates.value()
        fuzzy = self.phrase_panel.fuzzy.isChecked()
        self.statusBar().showMessage(
            "Reading captions…" if option.key == "builtin"
            else f"Asking {option.label.split(' —')[0]}…"
        )

        def work(ctx):
            return phrase_core.discover_with(
                phrase, engine, option=option, api_key=api_key,
                candidates=candidates, fuzzy=fuzzy,
                progress=lambda f, m="": ctx.progress(f, m),
                should_cancel=lambda: ctx.cancelled,
            )

        self.services.queue.submit(
            work, kind="phrase", label=f"Phrase: {phrase[:40]}"
        )

    def _on_hit_selected(self, hit) -> None:
        """Seek the embedded player to the hit. Nothing is downloaded to
        audition a phrase.

        A phrase hit is a timestamp, so this one does open the player: the
        whole point of choosing a hit is to hear that moment.
        """
        try:
            embed = self.providers["youtube"].preview(
                Result(provider="youtube", id=hit.video_id, title=hit.title,
                       url=hit.url),
                start=hit.start,
            )
            self.download_bar.show_preview(embed, hit.title)
            self._refresh_controls()
        except Exception as exc:  # noqa: BLE001
            log.warning("phrase preview failed: %r", exc)

    def _on_grab_hit(self, hit, start: float, end: float) -> None:
        """Cut the hit. Only the matched span transfers, not the whole video."""
        provider = self.providers["youtube"]
        choice = self.ask_export(
            mode="clip", subject=hit.matched[:60],
            source=SourceInfo.unknown(
                "a clip is cut from the same stream the video plays"
            ),
            audio_seconds=max(end - start, 0.0) or None,
        )
        if choice is None:
            return
        fmt = choice.fmt
        out_dir = Path(choice.dest_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = naming.resolve_output(
            out_dir, title=hit.matched, artist=hit.uploader,
            ext=fmt.ext or "wav",
        )
        auto_trim = self.phrase_panel.auto_trim.isChecked()
        result = Result(provider="youtube", id=hit.video_id, title=hit.matched,
                        url=hit.url)

        def work(ctx):
            written = provider.fetch(
                result, fmt, dest,
                progress=lambda f, m="": ctx.progress(f * 0.85, m),
                span=(start, end),
            )
            if not auto_trim:
                return written
            ctx.progress(0.9, "trimming")
            span = convert.tighten(written, min_silence=0.15, threshold_db=-38)
            if span is None or span.duration < 0.05:
                return written
            trimmed = written.with_name(
                f"{written.stem}-trimmed{written.suffix}"
            )
            convert.transcode(written, trimmed, fmt,
                              start=span.start, end=span.end)
            written.unlink(missing_ok=True)
            return trimmed

        job = self.services.queue.submit(
            work, kind="download", label=f"Clip: {hit.matched[:36]}"
        )
        if choice.separates:
            self._pending_stems[job] = (result, choice)
        self.statusBar().showMessage(f"Cutting “{hit.matched}”")

    def _ensure_soulseek(self) -> bool:
        """Connect to slskd, or explain what is missing.

        Deliberately not automatic on startup: starting slskd logs into
        Soulseek, which knocks the user's desktop client offline. That should
        happen when they ask for it, not because they opened the app.
        """
        provider = self.providers["soulseek"]
        if provider.connected():
            return True

        state = self.slskd.status()
        if not state.installed or not state.configured or not state.running:
            self.statusBar().showMessage(state.detail)
            self.open_settings()
            return False

        try:
            provider.connect(self.slskd.url, self.slskd.api_key() or "")
        except SoulseekUnavailable as exc:
            self.statusBar().showMessage(str(exc))
            return False
        return provider.connected()

    def _ensure_lucida(self) -> bool:
        """Check lucida-flow is here before searching, not after.

        The server starts itself on the first request, so this is only about
        the case it cannot recover from: no checkout to start. Saying that in
        the status bar and opening Settings — where the exact commands are —
        beats a job failing with a four-line install hint truncated into one.
        """
        if self.lucida.installed():
            return True
        self.statusBar().showMessage(
            f"The Spotify tab needs lucida-flow at {self.lucida.root} — "
            "see Settings"
        )
        self.open_settings()
        return False

    def open_settings(self) -> None:
        """Show the modeless settings window without changing app pages."""
        self.settings_dialog.show_settings()

    def _on_settings_saved(self) -> None:
        """Refresh non-secret state after settings are saved.

        Provider engines carry ordinary settings such as the YouTube cookie
        path. Paid-engine credentials stay lazy and are resolved only when the
        corresponding operation is explicitly started.
        """
        self._update_folder_label()
        self.providers = self._build_providers()
        self._refresh_phrase_engine()

    def _on_shuffled(self, tracks) -> None:
        if self.phrase_panel.enabled.isChecked():
            self.phrase_panel.enabled.setChecked(False)
        self.model.set_results([t.to_result() for t in tracks])
        self.statusBar().showMessage(f"{len(tracks)} from the crate")
        if tracks:
            self.table.selectRow(0)

    # -- previewing -------------------------------------------------------

    def preview_current_result(self) -> None:
        """The search page's Preview button."""
        result = self.model.result_at(self.table.currentIndex().row())
        if result is None:
            self.statusBar().showMessage("Select a result to preview it.")
            return
        self._show_preview(result)

    def preview_result_at(self, index) -> None:
        """Double-clicking a row previews that exact result."""
        result = self.model.result_at(index.row())
        if result is None:
            return
        self._show_preview(result)

    def preview_selected(self) -> None:
        """The tray's Preview button.

        A file in the tray wins over a row in the result list: if you have
        just selected one of four finished stems, that is plainly the thing
        you want to hear. With nothing selected there, it falls back to the
        result you are looking at, which streams rather than downloading.
        """
        paths = self.tray.list.selected_paths()
        if paths:
            path = Path(paths[0])
            self.download_bar.show_preview(
                LocalFile(path=path, temporary=False), path.name
            )
            self._refresh_controls()
            return

        result = self.model.result_at(self.table.currentIndex().row())
        if result is None:
            self.preview_current_result()
            return
        self._show_preview(result)

    def _show_preview(self, result: Result) -> None:
        provider = self.providers.get(result.provider, self.provider)
        if provider.preview_requires_transfer:
            if self._preview_job is not None:
                self.services.queue.cancel(self._preview_job)
            self._preview_result = result
            self.download_bar.show_preview_loading(result.title)

            def work(ctx):
                ctx.progress(0.05, "fetching preview")
                preview = provider.preview(result)
                ctx.check_cancelled()
                return preview

            self._preview_job = self.services.queue.submit(
                work, kind="preview", label=f"Preview: {result.title[:40]}"
            )
            self._refresh_controls()
            return

        try:
            preview = provider.preview(result)
            self.download_bar.show_preview(
                preview, result.title, result.artist, result.duration
            )
        except NotSupported as exc:
            self.download_bar.set_selection(
                result.title, result.artist, result.duration
            )
            self.download_bar.show_error(str(exc))
        except Exception as exc:  # noqa: BLE001 — a bad embed must not crash
            log.warning("preview failed: %r", exc)
            self.download_bar.set_selection(
                result.title, result.artist, result.duration
            )
            self.download_bar.show_error("No preview for this result.")
        self._refresh_controls()

    def stop_preview(self) -> None:
        if self._preview_job is not None:
            self.services.queue.cancel(self._preview_job)
            self._preview_job = None
            self._preview_result = None
        self.download_bar.stop_preview()
        self._refresh_controls()

    # -- selection and probing --------------------------------------------

    def _on_row_selected(self, current, _previous) -> None:
        row = current.row()
        result = self.model.result_at(row)
        self.set_download_available(result is not None)
        if result is None:
            return
        self.download_bar.set_selection(
            result.title, result.artist, result.duration
        )

        if self.model.media_at(row) is not None:
            return

        provider = self.provider

        def work(ctx):
            ctx.progress(0.3, "reading formats")
            return provider.probe(result)

        job = self.services.queue.submit(
            work, kind="probe", label=f"Formats: {result.title[:40]}"
        )
        self._probe_jobs[job] = row

    # -- separation -------------------------------------------------------

    def _queue_separation(
        self, audio: Path, result: Result, choice: ExportChoice
    ) -> None:
        """Run the chosen presets over a finished file and deliver the stems.

        Chained off the download rather than run beside it: the file has to
        exist before it can be separated, and BS-Roformer already saturates
        the CPU, so overlapping them would only make both slower.
        """
        if not choice.separates:
            return
        # Credential access belongs to this explicit action, never startup.
        separator = stems_core.separator_for(self.settings, self.calibration)
        if not separator.available():
            # The reason belongs to the engine: one of them is missing a build,
            # the other is missing a key.
            self.statusBar().showMessage(separator.unavailable_note)
            return

        keys = list(choice.stems)
        fmt = choice.fmt
        out_dir = Path(choice.dest_dir)
        scratch = self.services.paths.cache / "stems"
        duration = result.duration

        def work(ctx):
            raw = separator.separate(
                audio, keys, scratch / audio.stem,
                audio_seconds=duration,
                progress=lambda f, m="": ctx.progress(f * 0.95, m),
                should_cancel=lambda: ctx.cancelled,
            )
            # UVR writes WAV at whatever rate the input had. The chosen export
            # format is a promise about the file that lands in the download
            # folder, so it is honoured here rather than assumed.
            ctx.progress(0.96, "writing stems")
            written = {
                name: convert.transcode(
                    path, path.with_name(f"{path.stem}.out.{fmt.ext}"), fmt
                )
                for name, path in raw.items()
            }
            return stems_core.deliver(
                written, out_dir, title=result.title, artist=result.artist,
                ext=fmt.ext or "wav",
            )

        self.services.queue.submit(
            work, kind="stems",
            label=f"Stems: {result.title[:36]}",
        )

    def separate_selected(self) -> None:
        """Separate a file already in the tray, without downloading again.

        Reached from the tray's context menu. Downloading is the ordinary way
        to ask for stems — the export dialog carries the picker — so this is
        only for a file that is already here.
        """
        paths = self.tray.list.selected_paths() or self.tray.paths()[:1]
        if not paths:
            self.statusBar().showMessage(
                "Download something first, or select a finished file."
            )
            return
        audio = Path(paths[0])
        stem_name = audio.stem
        duration = None
        try:
            duration = convert.probe(audio).duration
        except Exception:  # noqa: BLE001 — an estimate is optional
            pass
        result = Result(
            provider=self.tab_key, id="", title=stem_name, duration=duration,
        )
        choice = self.ask_export(
            mode="separate", subject=audio.name,
            source=SourceInfo.from_file(audio), audio_seconds=duration,
        )
        if choice is None:
            return
        self._queue_separation(audio, result, choice)

    # -- downloading ------------------------------------------------------

    def download_selected(self) -> None:
        row = self.table.currentIndex().row()
        result = self.model.result_at(row)
        if result is None:
            return

        media = self.model.media_at(row)
        choice = self.ask_export(
            mode="download", subject=result.title[:60], media=media,
            audio_seconds=result.duration,
        )
        if choice is None:
            return

        fmt = choice.fmt
        out_dir = Path(choice.dest_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = naming.resolve_output(
            out_dir, title=result.title, artist=result.artist,
            ext=fmt.ext or "audio",
        )
        provider = self.provider

        def work(ctx):
            def progress(fraction, message=""):
                ctx.check_cancelled()
                ctx.progress(fraction, message)

            return provider.fetch(result, fmt, dest, progress=progress)

        job = self.services.queue.submit(
            work, kind="download",
            label=f"{fmt.key}: {result.title[:40]}", max_retries=1,
        )
        if choice.separates:
            self._pending_stems[job] = (result, choice)
        self.statusBar().showMessage(f"Queued {result.title}")

    def _update_folder_label(self) -> None:
        path = Path(self.settings.download_dir)
        self.download_bar.set_destination(f"→ {path.name or path}", str(path))
        self.tray.set_folder(path)

    def delete_files(self, paths) -> None:
        """Move files to the Trash, and say so.

        No dialog for the Trash: it is recoverable, and the Finder does not
        ask either. A system with no Trash to move them to is the one case
        that gets a question, because there unlinking is the whole of it.
        """
        paths = [Path(p) for p in paths]
        if not paths:
            return

        removed: list[Path] = []
        permanent = False
        for path in paths:
            try:
                trash.move_to_trash(path)
            except FileNotFoundError:
                removed.append(path)  # already gone; the row was stale
                continue
            except trash.TrashUnavailable as exc:
                log.warning("trash unavailable: %r", exc)
                if not permanent and not self._confirm_permanent_delete(paths):
                    break
                permanent = True
                try:
                    trash.delete(path)
                except OSError as failure:
                    self.statusBar().showMessage(f"Could not delete: {failure}")
                    continue
            removed.append(path)

        self.tray.remove_paths(removed)
        if removed:
            where = "deleted" if permanent else "moved to the Trash"
            name = removed[0].name if len(removed) == 1 else f"{len(removed)} files"
            self.statusBar().showMessage(f"{name} {where}")

    def _confirm_permanent_delete(self, paths) -> bool:
        answer = QMessageBox.question(
            self, "No Trash on this system",
            f"{len(paths)} file(s) cannot be moved to the Trash. "
            "Delete them permanently?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        return answer == QMessageBox.Yes

    def choose_download_folder(self) -> None:
        """The Downloaded page's button: pick where downloads go, and list
        what is already in there. One gesture, because "where do they land"
        and "what have I got" are the same question asked twice."""
        chosen = QFileDialog.getExistingDirectory(
            self, "Downloads folder", str(self.settings.download_dir)
        )
        if not chosen:
            return
        self.settings.download_dir = chosen
        self._update_folder_label()
        listed = self.tray.show_folder(Path(chosen))
        self.statusBar().showMessage(
            f"{listed} file(s) in {Path(chosen).name}" if listed
            else f"Nothing in {Path(chosen).name} yet"
        )

    # -- job results ------------------------------------------------------

    def _on_job_succeeded(self, snapshot) -> None:
        if snapshot.kind == "search":
            self.model.set_results(snapshot.result or [])
            self.statusBar().showMessage(f"{len(snapshot.result or [])} results")
        elif snapshot.kind == "phrase":
            search = snapshot.result
            self.phrase_panel.set_search(search)
            self.statusBar().showMessage(search.summary if search else "no hits")
        elif snapshot.kind == "probe":
            row = self._probe_jobs.pop(snapshot.id, None)
            if row is not None and isinstance(snapshot.result, Media):
                # The probe is what lets the export dialog state the source's
                # real format instead of "not known yet".
                self.model.apply_media(row, snapshot.result)
        elif snapshot.kind == "preview":
            if snapshot.id != self._preview_job:
                return
            result = self._preview_result
            self._preview_job = None
            self._preview_result = None
            if result is not None and isinstance(snapshot.result, (Embed, LocalFile)):
                self.download_bar.show_preview(
                    snapshot.result, result.title, result.artist, result.duration
                )
            else:
                self.download_bar.show_error("The preview did not produce audio.")
            self._refresh_controls()
        elif snapshot.kind == "download":
            path = snapshot.result
            if isinstance(path, Path):
                self.tray.add(path)
                self.note_arrival()
                self.statusBar().showMessage(f"Saved {path.name}")
                pending = self._pending_stems.pop(snapshot.id, None)
                if pending is not None:
                    self._queue_separation(path, *pending)
                if self._phrase_mode() and self.phrase_panel.current_hit:
                    self.phrase_panel.load_clip(path)
        elif snapshot.kind == "stems":
            delivered = snapshot.result or {}
            for name, path in sorted(delivered.items()):
                self.tray.add(path, subtitle=name)
            self.note_arrival(len(delivered))
            self.statusBar().showMessage(
                f"{len(delivered)} stem(s) ready" if delivered else "no stems produced"
            )

    def _on_job_failed(self, snapshot) -> None:
        self._probe_jobs.pop(snapshot.id, None)
        message = str(snapshot.error) if snapshot.error else "failed"
        self.statusBar().showMessage(message[:160])
        if snapshot.kind == "preview" and snapshot.id == self._preview_job:
            self._preview_job = None
            self._preview_result = None
            self.download_bar.show_error(message)
            self._refresh_controls()
        elif snapshot.kind == "download":
            log.error("download failed: %s", message)

    # -- shutdown ---------------------------------------------------------

    def closeEvent(self, event) -> None:
        active = [j for j in self.services.queue.jobs() if not j.state.terminal]
        if active:
            answer = QMessageBox.question(
                self, "Still working",
                f"{len(active)} job(s) are still running. Quit anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
        self.download_bar.stop_preview()
        self.bridge.close()
        self.providers["soulseek"].close()
        # Both daemons are ours; they go down with the app rather than being
        # left logged into Soulseek, or holding a headless browser open, after
        # the window closes.
        self.slskd.stop()
        self.lucida.stop()
        process = self._samplette_process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        super().closeEvent(event)
