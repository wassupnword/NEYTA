"""Settings: credentials, folders, and the Soulseek daemon.

A page in the window rather than a modal dialog. Settings is somewhere you go,
the same way the search and the downloads are — and it is where you are sent
when a service will not work until you have filled something in, which a modal
made into an interruption rather than a destination. Being a page also means
it is still there when you come back to it, and that the activity strip along
the bottom keeps running while you read it.

There is no OK button, because a page has nothing to dismiss: what is on
screen is written when you leave, and re-read from where it actually lives
when you arrive. The Save button is the same act done early.

Rendered from the SERVICES declaration in neyta/settings.py rather than from a
hand-written form, so a field added there appears here, is stored in the right
place, and is cleared by the wipe — without three lists drifting apart.

Every field is re-editable forever. Secrets go to the macOS Keychain; nothing
sensitive is written to disk in plaintext by NEYTA itself. The one exception
is slskd.yml, which has to contain the Soulseek password because the daemon
has no other way to receive it — that file is written 0600 in the app's
support directory, and the section that writes it says so.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea, QVBoxLayout,
    QWidget,
)

from .. import config
from .. import settings as settings_mod
from .widgets import centred_row

log = logging.getLogger(__name__)


class PathField(QWidget):
    """A line edit with a Browse button."""

    changed = Signal(str)

    def __init__(self, directory: bool = True, parent=None) -> None:
        super().__init__(parent)
        self.directory = directory
        self.edit = QLineEdit()
        self.edit.textChanged.connect(self.changed)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.edit, 1)
        layout.addWidget(browse)

    def _browse(self) -> None:
        if self.directory:
            chosen = QFileDialog.getExistingDirectory(
                self, "Choose a folder", self.edit.text() or str(Path.home())
            )
        else:
            chosen, _ = QFileDialog.getOpenFileName(
                self, "Choose a file", self.edit.text() or str(Path.home())
            )
        if chosen:
            self.edit.setText(chosen)

    def text(self) -> str:
        return self.edit.text().strip()

    def setText(self, value: str) -> None:
        self.edit.setText(value or "")


class ServiceSection(QGroupBox):
    """One service's fields, plus its Clear button."""

    changed = Signal()

    def __init__(self, spec: settings_mod.ServiceSpec, settings, parent=None):
        super().__init__(spec.label, parent)
        self.spec = spec
        self.settings = settings
        self.fields: dict[str, QWidget] = {}

        form = QFormLayout()
        for field in spec.fields:
            widget = self._build_field(field)
            self.fields[field.name] = widget
            label = field.label + (" *" if field.required else "")
            form.addRow(label, widget)
            if field.help:
                hint = QLabel(field.help)
                hint.setWordWrap(True)
                hint.setStyleSheet("color: palette(mid); font-size: 11px;")
                form.addRow("", hint)

        note = QLabel(spec.note)
        note.setWordWrap(True)
        note.setStyleSheet("color: palette(mid);")

        clear = QPushButton("Clear")
        clear.setFixedWidth(80)
        clear.clicked.connect(self._clear)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(clear)

        layout = QVBoxLayout(self)
        if spec.note:
            layout.addWidget(note)
        layout.addLayout(form)
        layout.addLayout(buttons)

        self.load()

    def _build_field(self, field: settings_mod.CredField) -> QWidget:
        if field.kind in ("path", "file"):
            widget = PathField(directory=field.kind == "path")
            widget.changed.connect(self.changed)
            return widget
        edit = QLineEdit()
        if field.kind == "password":
            edit.setEchoMode(QLineEdit.Password)
        edit.textChanged.connect(self.changed)
        return edit

    def load(self) -> None:
        for field in self.spec.fields:
            widget = self.fields[field.name]
            if field.secret:
                # Reading a Keychain item can show a macOS authorization
                # dialog. Never fan those dialogs out merely because the
                # settings page was constructed or opened.
                widget.setText("")
                if isinstance(widget, QLineEdit):
                    widget.setPlaceholderText(
                        "Saved in Keychain — enter a new value to replace it"
                    )
                continue
            value = self.settings.credential(self.spec.key, field.name) or ""
            widget.setText(value)

    def save(self) -> None:
        for field in self.spec.fields:
            value = self.fields[field.name].text()
            # A blank masked field means "leave the saved value alone". The
            # adjacent Clear button remains the explicit way to delete it.
            if field.secret and not value:
                continue
            self.settings.set_credential(self.spec.key, field.name, value)

    def _clear(self) -> None:
        self.settings.clear_service(self.spec.key)
        self.load()
        self.changed.emit()

    def values(self) -> dict[str, str]:
        return {name: widget.text() for name, widget in self.fields.items()}


class SoulseekSection(QWidget):
    """The daemon's state, and the buttons that change it.

    Soulseek is the one service where entering a password is not enough:
    something has to be running and logged into the network. Putting install,
    start and status next to the credentials means the whole story is in one
    place instead of split between a dialog and a tab that says "not
    connected".
    """

    changed = Signal()

    def __init__(self, bootstrap, settings, parent=None) -> None:
        super().__init__(parent)
        self.bootstrap = bootstrap
        self.settings = settings

        self.status = QLabel("")
        self.status.setWordWrap(True)

        self.warning = QLabel(
            "Soulseek allows one login per account, so starting this will "
            "usually disconnect the Soulseek app on your desktop."
        )
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet("color: #b5730a;")

        self.install_button = QPushButton("Install slskd (~58 MB)")
        self.start_button = QPushButton("Start")
        self.stop_button = QPushButton("Stop")
        for button in (self.install_button, self.start_button, self.stop_button):
            button.setAutoDefault(False)

        buttons = QHBoxLayout()
        buttons.addWidget(self.install_button)
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.stop_button)
        buttons.addStretch(1)

        box = QGroupBox("Soulseek daemon")
        inner = QVBoxLayout(box)
        inner.addWidget(QLabel(
            "Soulseek has no HTTP API, so NEYTA drives slskd — a headless "
            "Soulseek client. It is a different program from the Soulseek "
            "app; having one does not provide the other."
        ))
        inner.itemAt(0).widget().setWordWrap(True)
        inner.addWidget(self.status)
        inner.addWidget(self.warning)
        inner.addLayout(buttons)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(box)
        self.refresh()

    def refresh(self) -> None:
        state = self.bootstrap.status()
        marks = {True: "●", False: "○"}
        self.status.setText(
            f"{marks[state.running]} {state.detail}"
            + (f"\nbinary: {state.binary}" if state.binary else "")
        )
        self.status.setStyleSheet(
            "color: #2a8a4a;" if state.running else "color: palette(mid);"
        )
        self.install_button.setVisible(not state.installed)
        self.start_button.setEnabled(
            state.installed and state.configured and not state.running
        )
        self.stop_button.setEnabled(self.bootstrap.is_running())
        self.warning.setVisible(not state.running)


class LucidaSection(QWidget):
    """Where the Spotify tab comes from, and whether it is here.

    The same reasoning as the Soulseek daemon block above it: the tab depends
    on a separate program, and "no results" is a useless thing to be told when
    the real answer is that the program was never installed. Unlike slskd,
    NEYTA does not fetch this one — it is a source checkout with a browser
    engine behind it — so this section's job is to say precisely what to run.
    """

    def __init__(self, bootstrap, parent=None) -> None:
        super().__init__(parent)
        self.bootstrap = bootstrap

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.hint = QLabel("")
        self.hint.setWordWrap(True)
        self.hint.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.hint.setStyleSheet(
            "color: palette(mid); font-family: monospace; font-size: 11px;"
        )

        self.start_button = QPushButton("Start")
        self.stop_button = QPushButton("Stop")
        for button in (self.start_button, self.stop_button):
            button.setAutoDefault(False)
        buttons = QHBoxLayout()
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.stop_button)
        buttons.addStretch(1)

        box = QGroupBox("Spotify tab (lucida-flow)")
        inner = QVBoxLayout(box)
        blurb = QLabel(
            "The Spotify tab is a client of lucida-flow, which fetches from "
            "streaming services through lucida.to. It runs as a local server "
            "beside NEYTA and starts on its own the first time you use the "
            "tab. This tab takes paid streaming catalogue through a "
            "third-party ripper — a different footing from the other four."
        )
        blurb.setWordWrap(True)
        inner.addWidget(blurb)
        inner.addWidget(self.status)
        inner.addWidget(self.hint)
        inner.addLayout(buttons)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(box)
        self.refresh()

    def refresh(self) -> None:
        state = self.bootstrap.status()
        marks = {True: "●", False: "○"}
        self.status.setText(f"{marks[state.running]} {state.detail}")
        self.status.setStyleSheet(
            "color: #2a8a4a;" if state.running else "color: palette(mid);"
        )
        self.hint.setVisible(not state.installed)
        self.hint.setText("" if state.installed else self.bootstrap.setup_hint)
        self.start_button.setEnabled(state.installed and not state.running)
        self.stop_button.setEnabled(self.bootstrap.is_running())


class EngineChooser(QGroupBox):
    """Pick which engine does one of the two swappable jobs.

    Rendered from a tuple of EngineOption rather than written out twice, so
    phrase search and separation offer their choices the same way and a third
    swappable job would need no new widget.

    A paid engine whose key is missing is shown, selectable, and marked — the
    point of the list is to say what is possible, and hiding the upgrade until
    you already have it is the wrong way round. What it must not do is quietly
    become the engine that runs: `Settings` falls back to the free one until
    the key is there, and the note under the box says so.
    """

    def __init__(self, title: str, options, settings, current: str, parent=None):
        super().__init__(title, parent)
        self.settings = settings
        self.options = tuple(options)

        self.box = QComboBox()
        for option in self.options:
            self.box.addItem(option.label, option.key)
        self.note = QLabel("")
        self.note.setWordWrap(True)
        self.note.setStyleSheet("color: palette(mid);")

        # Only ever shown for an engine that bills. What you want to know
        # about a metered service is how much of it is left, and the only
        # honest source for that is the service.
        self.check_button = QPushButton("Check plan")
        self.check_button.setAutoDefault(False)
        self.check_button.setVisible(False)
        row = QHBoxLayout()
        row.addWidget(self.box, 1)
        row.addWidget(self.check_button)

        layout = QVBoxLayout(self)
        layout.addLayout(row)
        layout.addWidget(self.note)

        self.set_current(current)
        self.box.currentIndexChanged.connect(self._on_changed)

    def current(self) -> str:
        return str(self.box.currentData())

    def current_option(self):
        return next(o for o in self.options if o.key == self.current())

    def set_current(self, key: str) -> None:
        index = self.box.findData(key)
        self.box.setCurrentIndex(index if index >= 0 else 0)
        self._refresh_note()

    def _on_changed(self, _index: int) -> None:
        self._refresh_note()

    def _refresh_note(self) -> None:
        option = self.current_option()
        text = option.note
        if option.service:
            text += (
                f"\n\nEnter or replace the {option.service.upper()} key below "
                "if needed. Saved credentials stay in Keychain and are only "
                "requested when this service runs."
            )
        self.note.setText(text)
        self.check_button.setVisible(option.paid)

    def show_note(self, text: str) -> None:
        """Say something specific in place of the standing description — the
        answer from a Check plan, or why there wasn't one."""
        self.note.setText(text)


class SettingsPage(QWidget):
    """Everything re-editable, in one place."""

    #: The values have been written. The window rebuilds what depends on them
    #: — the engines carry the cookie file, the tray shows the folder.
    saved = Signal()

    #: The form's own width. The window is wide enough for a result list; a
    #: form stretched to match it is a line of labels a long way from the
    #: fields they belong to.
    FORM_WIDTH = 720

    def __init__(self, settings, bootstrap=None, cache=None, lucida=None,
                 parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.bootstrap = bootstrap
        self.cache = cache
        self.lucida = lucida

        self.downloads = PathField()
        self.downloads.setText(str(settings.download_dir))

        general = QGroupBox("Files")
        general_form = QFormLayout(general)
        general_form.addRow("Save downloads to", self.downloads)

        # The two jobs that can be done here or bought. Above the credentials
        # they depend on, because the choice is the point and the key is the
        # paperwork.
        self.phrase_engine = EngineChooser(
            "Phrase search engine", config.PHRASE_ENGINES, settings,
            settings.get("phrase/engine", config.DEFAULT_PHRASE_ENGINE),
        )
        self.stem_engine = EngineChooser(
            "Stem separation engine", config.STEM_ENGINES, settings,
            settings.get("stems/engine", config.DEFAULT_STEM_ENGINE),
        )

        self.sections: dict[str, ServiceSection] = {}
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.addWidget(general)
        body_layout.addWidget(self.phrase_engine)
        body_layout.addWidget(self.stem_engine)

        for spec in settings_mod.SERVICES:
            section = ServiceSection(spec, settings)
            self.sections[spec.key] = section
            body_layout.addWidget(section)
            if spec.key == "soulseek" and bootstrap is not None:
                self.soulseek_daemon = SoulseekSection(bootstrap, settings)
                body_layout.addWidget(self.soulseek_daemon)
                section.changed.connect(self._on_soulseek_changed)
                self.soulseek_daemon.install_button.clicked.connect(self._install)
                self.soulseek_daemon.start_button.clicked.connect(self._start)
                self.soulseek_daemon.stop_button.clicked.connect(self._stop)
            if spec.key == "youtube":
                self.use_cookies = QCheckBox(
                    "Send this cookies file with YouTube requests"
                )
                self.use_cookies.setChecked(bool(settings.get("youtube/use_cookies")))
                self.use_cookies.setToolTip(
                    "Off by default. Everything works without it; automated "
                    "access with account cookies runs against YouTube's terms "
                    "and carries some account risk."
                )
                body_layout.addWidget(self.use_cookies)

        if lucida is not None:
            self.lucida_section = LucidaSection(lucida)
            self.lucida_section.start_button.clicked.connect(self._start_lucida)
            self.lucida_section.stop_button.clicked.connect(self._stop_lucida)
            body_layout.addWidget(self.lucida_section)

        self.stem_engine.check_button.clicked.connect(self._check_stem_plan)

        body_layout.addStretch(1)
        body.setMaximumWidth(self.FORM_WIDTH)

        # The form on the window's centre line, like the search field above
        # the results, rather than pinned to the left of a wide window. It
        # takes most of a narrow window and stops at FORM_WIDTH on a wide one,
        # which is what the weight against the two margins buys.
        holder = QWidget()
        holder.setLayout(centred_row(body, weight=4))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(holder)

        self.wipe_button = QPushButton("Wipe everything…")
        self.wipe_button.clicked.connect(self._wipe)
        self.save_button = QPushButton("Save")
        self.save_button.setAutoDefault(False)
        self.save_button.clicked.connect(self.save)
        # Wipe at the far end from Save: they are both endings, and the one
        # that cannot be undone should not be next to the one you press every
        # time.
        footer = QHBoxLayout()
        footer.addWidget(self.wipe_button)
        footer.addStretch(1)
        footer.addWidget(self.save_button)

        layout = QVBoxLayout(self)
        layout.addWidget(scroll, 1)
        layout.addLayout(footer)

    # -- persistence ------------------------------------------------------

    def save(self) -> None:
        # Credentials first: an engine chosen in the same visit as the key it
        # needs must find that key already in the Keychain, or it would fall
        # back to the free one on the way out and read as not having saved.
        for section in self.sections.values():
            section.save()
        if self.downloads.text():
            self.settings.download_dir = self.downloads.text()
        if hasattr(self, "use_cookies"):
            self.settings.set("youtube/use_cookies", self.use_cookies.isChecked())
        self.settings.phrase_engine = self.phrase_engine.current()
        self.settings.stem_engine = self.stem_engine.current()
        self._write_slskd_config()
        self.saved.emit()

    def reload(self) -> None:
        """Take every value from where it actually lives.

        A page outlives the visit that built it, and the download folder can
        be changed from the downloaded page while this one is off screen — so
        arriving re-reads rather than trusting what is in the boxes.
        """
        for section in self.sections.values():
            section.load()
        self.downloads.setText(str(self.settings.download_dir))
        if hasattr(self, "use_cookies"):
            self.use_cookies.setChecked(
                bool(self.settings.get("youtube/use_cookies"))
            )
        # The stored key, not the engine actually in force: those differ while
        # a paid engine is chosen and its key is missing, and this box is
        # where that choice is made rather than where it is applied.
        self.phrase_engine.set_current(
            self.settings.get("phrase/engine", config.DEFAULT_PHRASE_ENGINE)
        )
        self.stem_engine.set_current(
            self.settings.get("stems/engine", config.DEFAULT_STEM_ENGINE)
        )
        self._on_soulseek_changed()

    def _on_soulseek_changed(self) -> None:
        if hasattr(self, "soulseek_daemon"):
            self.soulseek_daemon.refresh()
        if hasattr(self, "lucida_section"):
            self.lucida_section.refresh()

    # -- the paid engines -------------------------------------------------

    def _check_stem_plan(self) -> None:
        """Ask LALAL.AI what is left on the plan.

        Reads the key out of the field rather than the store, so a key just
        pasted can be checked before it has been saved — which is the moment
        you actually want to know whether it works.
        """
        from ..core.lalal import LalalError, LalalSeparator

        key = self.sections["lalal"].values().get("api_key", "")
        if not key:
            # Check plan is an explicit request to use this credential, unlike
            # opening the app or visiting Settings.
            key = self.settings.credential("lalal", "api_key") or ""
        if not key:
            self.stem_engine.show_note("Paste a licence key below first.")
            return
        try:
            limits = LalalSeparator(api_key=key).limits()
        except LalalError as exc:
            self.stem_engine.show_note(str(exc))
            return
        self.stem_engine.show_note(f"{limits.email} — {limits.describe}")

    # -- the Spotify server -----------------------------------------------

    def _start_lucida(self) -> None:
        try:
            self.lucida.start()
        except Exception as exc:  # noqa: BLE001 — a message, not a traceback
            QMessageBox.warning(self, "lucida-flow", str(exc))
        self.lucida_section.refresh()

    def _stop_lucida(self) -> None:
        self.lucida.stop()
        self.lucida_section.refresh()

    def _write_slskd_config(self) -> bool:
        """Push the credentials into slskd.yml.

        Returns False when the login is incomplete, which is not an error —
        the other three tabs work without it, so an unfinished Soulseek
        section must not block saving anything else.
        """
        if self.bootstrap is None:
            return False
        values = self.sections["soulseek"].values()
        if not all(values.get(k) for k in ("username", "password", "share_dir")):
            return False
        try:
            self.bootstrap.write_config(
                username=values["username"],
                password=values["password"],
                share_dirs=[values["share_dir"]],
                download_dir=Path(self.settings.download_dir) / "Soulseek",
            )
            return True
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Soulseek", str(exc))
            return False

    # -- daemon -----------------------------------------------------------

    def _install(self) -> None:
        answer = QMessageBox.question(
            self, "Install slskd",
            "Download slskd (about 58 MB) from github.com/slskd/slskd?\n\n"
            "It is a self-contained binary — no Homebrew and no .NET runtime "
            "needed. It is stored inside NEYTA's own support folder.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            self.bootstrap.install()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Install failed", str(exc))
        self.soulseek_daemon.refresh()

    def _start(self) -> None:
        if not self._write_slskd_config():
            QMessageBox.information(
                self, "Soulseek",
                "Fill in your username, password and a folder to share first.\n\n"
                "The network bans clients that only take, so the shared folder "
                "has to point at real music.",
            )
            return
        try:
            self.bootstrap.start()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Could not start slskd", str(exc))
        self.soulseek_daemon.refresh()

    def _stop(self) -> None:
        self.bootstrap.stop()
        self.soulseek_daemon.refresh()

    # -- wipe -------------------------------------------------------------

    def _wipe(self) -> None:
        answer = QMessageBox.warning(
            self, "Wipe everything",
            "Remove every saved credential from the Keychain, clear all "
            "preferences, and empty the caches?\n\n"
            "Music you have already downloaded is not touched.",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
        )
        if answer != QMessageBox.Yes:
            return
        removed = self.settings.wipe_everything(cache=self.cache)
        for section in self.sections.values():
            section.load()
        self.downloads.setText(str(self.settings.download_dir))
        QMessageBox.information(
            self, "Wiped",
            f"{removed.get('secrets', 0)} credential(s), "
            f"{removed.get('prefs', 0)} preference(s), "
            f"{removed.get('cache_rows', 0)} cache entries and "
            f"{removed.get('temp_files', 0)} temporary file(s) removed.",
        )
