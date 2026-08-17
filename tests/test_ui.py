"""The shell. Runs offscreen; asked for with `-m ui`.

The drag tests are the ones that matter most. Dragging into Ableton is the
whole reason NEYTA is a native app, and it is the easiest thing to break
silently: a payload with the wrong flavour, or with a URI pointing at nothing,
drops into Live as no clip and no error.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
pytestmark = pytest.mark.ui

from PySide6.QtCore import QMimeData, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from neyta import config  # noqa: E402
from neyta.core.jobs import JobQueue  # noqa: E402
from neyta.providers.base import Media, Result, Stream  # noqa: E402
from neyta.ui import dragout, results as results_ui  # noqa: E402
from neyta.ui.qtbridge import JobBridge  # noqa: E402
from neyta.ui.tray import DragTray, PathRole  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def audio_files(tmp_path):
    paths = []
    for name in ("A - Song [vocals].wav", "A - Song [drums].wav",
                 "A - Song [bass].wav"):
        p = tmp_path / name
        p.write_bytes(b"RIFF" + b"\0" * 128)
        paths.append(p)
    return paths


# ---------------------------------------------------------------------------
# The drag payload
# ---------------------------------------------------------------------------


class TestDragPayload:
    def test_it_carries_file_uris(self, qapp, audio_files):
        mime = dragout.build_mime(audio_files[:1])
        uris = dragout.uri_list(mime)
        assert len(uris) == 1
        assert uris[0].startswith("file://")

    def test_the_uri_resolves_back_to_the_same_file(self, qapp, audio_files):
        mime = dragout.build_mime([audio_files[0]])
        assert dragout.local_paths(mime) == [audio_files[0].resolve()]

    def test_it_sets_the_flavour_finder_and_live_read(self, qapp, audio_files):
        mime = dragout.build_mime(audio_files)
        assert mime.hasUrls()
        assert "text/uri-list" in mime.formats()

    def test_multi_select_carries_every_file_in_order(self, qapp, audio_files):
        mime = dragout.build_mime(audio_files)
        assert dragout.local_paths(mime) == [p.resolve() for p in audio_files]

    def test_plain_text_is_also_offered(self, qapp, audio_files):
        # So dropping into a text field or terminal yields usable paths.
        mime = dragout.build_mime([audio_files[0]])
        assert str(audio_files[0].resolve()) in mime.text()

    def test_spaces_and_brackets_in_names_survive(self, qapp, audio_files):
        # "Artist - Title [vocals].wav" is the standard output name.
        path = dragout.local_paths(dragout.build_mime([audio_files[0]]))[0]
        assert path.name == "A - Song [vocals].wav"

    def test_unicode_names_survive(self, qapp, tmp_path):
        path = tmp_path / "坂本龍一 - 戦場のメリークリスマス.wav"
        path.write_bytes(b"RIFF")
        assert dragout.local_paths(dragout.build_mime([path])) == [path.resolve()]

    def test_a_missing_file_is_dropped_rather_than_sent_as_a_dead_uri(
        self, qapp, tmp_path, audio_files
    ):
        # A URI pointing at nothing drops into Live as silence with no error.
        mime = dragout.build_mime([audio_files[0], tmp_path / "gone.wav"])
        assert dragout.local_paths(mime) == [audio_files[0].resolve()]

    def test_duplicates_are_collapsed(self, qapp, audio_files):
        mime = dragout.build_mime([audio_files[0], audio_files[0]])
        assert len(dragout.uri_list(mime)) == 1

    def test_a_directory_is_not_draggable_as_a_file(self, qapp, tmp_path):
        (tmp_path / "folder").mkdir()
        assert dragout.existing_paths([tmp_path / "folder"]) == []

    def test_an_empty_payload_has_no_urls(self, qapp, tmp_path):
        mime = dragout.build_mime([tmp_path / "nope.wav"])
        assert dragout.uri_list(mime) == []

    def test_starting_a_drag_with_nothing_is_a_no_op(self, qapp, tmp_path):
        widget = DragTray()
        assert dragout.start_drag(widget, [tmp_path / "nope.wav"]) == Qt.IgnoreAction


class TestDragTray:
    def test_a_finished_file_appears(self, qapp, audio_files):
        tray = DragTray()
        tray.add(audio_files[0])
        assert tray.count() == 1
        assert tray.paths() == [audio_files[0]]

    def test_a_missing_file_is_refused(self, qapp, tmp_path):
        tray = DragTray()
        assert tray.add(tmp_path / "nope.wav") is None
        assert tray.count() == 0

    def test_the_same_file_is_not_listed_twice(self, qapp, audio_files):
        tray = DragTray()
        tray.add(audio_files[0])
        tray.add(audio_files[0])
        assert tray.count() == 1

    def test_newest_first(self, qapp, audio_files):
        tray = DragTray()
        for p in audio_files:
            tray.add(p)
        assert tray.paths()[0] == audio_files[-1]

    def test_the_tray_builds_the_payload_from_its_selection(self, qapp, audio_files):
        tray = DragTray()
        for p in audio_files:
            tray.add(p)
        tray.list.selectAll()
        mime = tray.list.mimeData(tray.list.selectedItems())
        assert len(dragout.local_paths(mime)) == len(audio_files)

    def test_stems_from_one_separation_drag_together(self, qapp, audio_files):
        # The reason multi-select exists: four stems onto four adjacent tracks.
        tray = DragTray()
        for p in audio_files:
            tray.add(p)
        tray.list.selectAll()
        names = {p.name for p in dragout.local_paths(
            tray.list.mimeData(tray.list.selectedItems()))}
        assert names == {p.name for p in audio_files}

    def test_clearing_the_list_leaves_the_files_on_disk(self, qapp, audio_files):
        tray = DragTray()
        tray.add(audio_files[0])
        tray.clear()
        assert tray.count() == 0
        assert audio_files[0].exists()

    def test_items_remember_their_path(self, qapp, audio_files):
        tray = DragTray()
        tray.add(audio_files[0])
        assert tray.list.item(0).data(PathRole) == audio_files[0]

    def test_separating_a_finished_file_is_in_the_context_menu(self, qapp):
        # Not a button: Download is where a separation is normally asked for.
        tray = DragTray()
        actions = [a.text() for a in tray.build_menu().actions()]
        assert "Separate…" in actions

    def test_the_context_menu_asks_rather_than_separating_itself(
        self, qapp, audio_files
    ):
        tray = DragTray()
        tray.add(audio_files[0])
        asked = []
        tray.separate_requested.connect(lambda: asked.append(True))
        next(a for a in tray.build_menu().actions()
             if a.text() == "Separate…").trigger()
        assert asked == [True]

    def test_the_only_button_is_the_folder_one(self, qapp):
        # It is a folder: what you do to a file, you do by right-clicking it.
        from PySide6.QtWidgets import QPushButton

        tray = DragTray()
        assert [b.text() for b in tray.findChildren(QPushButton)] == ["Change…"]

    def test_everything_you_can_do_to_a_file_is_in_the_menu(self, qapp):
        tray = DragTray()
        actions = [a.text() for a in tray.build_menu().actions() if a.text()]
        assert actions == [
            "Preview", "Separate…", "Reveal in Finder", "Copy path",
            "Move to Trash",
        ]

    def test_deleting_asks_the_window_rather_than_unlinking_itself(
        self, qapp, audio_files
    ):
        tray = DragTray()
        tray.add(audio_files[0])
        tray.list.selectAll()
        asked = []
        tray.delete_requested.connect(asked.append)
        next(a for a in tray.build_menu().actions()
             if a.text() == "Move to Trash").trigger()
        assert asked == [[audio_files[0]]]
        assert audio_files[0].exists(), "the tray does not touch the disk"

    def test_rows_can_be_dropped_once_their_files_are_gone(
        self, qapp, audio_files
    ):
        tray = DragTray()
        for path in audio_files:
            tray.add(path)
        tray.remove_paths(audio_files[:2])
        assert tray.paths() == [audio_files[2]]

    def test_there_is_no_clearing_a_folder(self, qapp):
        tray = DragTray()
        assert "Clear list" not in [a.text() for a in tray.build_menu().actions()]

    def test_it_opens_with_the_same_field_and_button_as_the_search_page(
        self, qapp
    ):
        from neyta.ui.widgets import FIELD_BUTTON_WIDTH, FIELD_WIDTH

        tray = DragTray()
        for row, widget in ((tray.path_row, tray.path_field),
                            (tray.button_row, tray.folder_button)):
            assert row.indexOf(widget) == 1 and row.count() == 3
        assert tray.path_field.maximumWidth() == FIELD_WIDTH
        assert tray.folder_button.width() == FIELD_BUTTON_WIDTH

    def test_the_path_is_shown_but_not_typed_into(self, qapp, tmp_path):
        tray = DragTray()
        tray.set_folder(tmp_path)
        assert tray.path_field.text() == str(tmp_path)
        assert tray.path_field.isReadOnly()

    def test_the_button_asks_rather_than_changing_the_setting_itself(
        self, qapp
    ):
        tray = DragTray()
        asked = []
        tray.folder_change_requested.connect(lambda: asked.append(True))
        tray.folder_button.click()
        assert asked == [True]

    def test_showing_a_folder_lists_the_audio_in_it(self, qapp, tmp_path):
        for name in ("a.wav", "b.mp3", "c.flac"):
            (tmp_path / name).write_bytes(b"RIFF")
        (tmp_path / "notes.txt").write_text("not audio")
        (tmp_path / "subfolder").mkdir()

        tray = DragTray()
        assert tray.show_folder(tmp_path) == 3
        assert {p.name for p in tray.paths()} == {"a.wav", "b.mp3", "c.flac"}
        assert tray.path_field.text() == str(tmp_path)

    def test_showing_a_folder_lists_the_newest_first(self, qapp, tmp_path):
        import os
        import time

        for index, name in enumerate(("old.wav", "new.wav")):
            path = tmp_path / name
            path.write_bytes(b"RIFF")
            os.utime(path, (time.time() + index, time.time() + index))
        tray = DragTray()
        tray.show_folder(tmp_path)
        assert tray.paths()[0].name == "new.wav"

    def test_showing_a_folder_replaces_what_was_listed(self, qapp, tmp_path,
                                                       audio_files):
        tray = DragTray()
        tray.add(audio_files[0])
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "only.wav").write_bytes(b"RIFF")
        tray.show_folder(elsewhere)
        assert [p.name for p in tray.paths()] == ["only.wav"]

    def test_a_folder_that_is_not_there_lists_nothing_and_does_not_raise(
        self, qapp, tmp_path
    ):
        tray = DragTray()
        assert tray.show_folder(tmp_path / "gone") == 0
        assert tray.count() == 0

    def test_a_huge_folder_is_capped(self, qapp, tmp_path):
        for i in range(20):
            (tmp_path / f"{i:03d}.wav").write_bytes(b"RIFF")
        tray = DragTray()
        assert tray.show_folder(tmp_path, limit=5) == 5

    def test_the_tray_asks_rather_than_owning_a_player(self, qapp, audio_files):
        tray = DragTray()
        tray.add(audio_files[0])
        asked = []
        tray.preview_requested.connect(lambda: asked.append(True))
        next(a for a in tray.build_menu().actions()
             if a.text() == "Preview").trigger()
        assert asked == [True]


# ---------------------------------------------------------------------------
# The result list
# ---------------------------------------------------------------------------


def cell(grid, widget):
    """(row, column) of a widget in a QGridLayout."""
    return grid.getItemPosition(grid.indexOf(widget))[:2]


def badge(window):
    """How many arrivals the folder icon is showing."""
    from neyta.ui.window import PAGE_DOWNLOADED

    return window.page_bar.buttons[PAGE_DOWNLOADED].badge()


def make_result(**kw):
    base = dict(provider="youtube", id="x", title="A Track", artist="An Artist")
    base.update(kw)
    return Result(**base)


class FakeShuffleTrack:
    def __init__(self, result=None):
        self._result = result or make_result()

    def to_result(self):
        return self._result


class FakeShuffleLibrary:
    @classmethod
    def available(cls):
        return True

    def facet(self, _name, _limit):
        return [("Brazil", 2), ("MPB", 1)]

    def bounds(self, name):
        return (90, 140) if name == "tempo" else (1970, 1980)

    def stats(self):
        return type("Stats", (), {"ready": 2, "total": 3})()

    def count(self, _filters=None):
        return 2

    def sample(self, n, _filters=None, mode="shuffle"):
        return [
            FakeShuffleTrack(make_result(id=f"vid-{mode}-{i}", title=f"Track {i}"))
            for i in range(n)
        ]

    def close(self):
        pass


def attach_shuffle_library(window):
    window.shuffle_panel.attach(FakeShuffleLibrary())
    window._refresh_shuffle_controls()


class TestResultModel:
    def test_rows_and_columns(self, qapp):
        model = results_ui.ResultModel()
        model.set_results([make_result(), make_result()])
        assert model.rowCount() == 2
        assert model.columnCount() == len(results_ui.COLUMNS)

    def test_title_column_joins_artist_and_title(self, qapp):
        model = results_ui.ResultModel()
        model.set_results([make_result()])
        assert model.index(0, results_ui.COL_TITLE).data() == "An Artist — A Track"

    def test_a_result_with_no_artist_shows_just_the_title(self, qapp):
        model = results_ui.ResultModel()
        model.set_results([make_result(artist=None)])
        assert model.index(0, results_ui.COL_TITLE).data() == "A Track"

    @pytest.mark.parametrize(
        "seconds,expected",
        [(None, "—"), (0, "—"), (61, "1:01"), (599, "9:59"), (3661, "1:01:01")],
    )
    def test_duration_formatting(self, qapp, seconds, expected):
        assert results_ui.format_duration(seconds) == expected

    def test_unknown_bitrate_reads_as_a_dash_not_zero(self, qapp):
        model = results_ui.ResultModel()
        model.set_results([make_result(source_kbps=None)])
        assert model.quality_label(0) == "—"

    def test_a_known_bitrate_is_shown(self, qapp):
        model = results_ui.ResultModel()
        model.set_results([make_result(source_kbps=129.8)])
        assert model.quality_label(0) == "130k"

    def test_probing_fills_in_the_true_bitrate_in_place(self, qapp):
        model = results_ui.ResultModel()
        model.set_results([make_result(source_kbps=None)])
        media = Media(
            result=make_result(title="A Track", source_kbps=129.0),
            streams=(Stream(id="140", ext="m4a", bitrate_kbps=129.0, codec="aac"),),
        )
        model.apply_media(0, media)
        assert model.quality_label(0) == "129k"
        assert model.rowCount() == 1, "the row must not be duplicated"

    def test_a_lossless_probe_shows_a_badge_not_a_number(self, qapp):
        model = results_ui.ResultModel()
        model.set_results([make_result(provider="bandcamp", source_kbps=None)])
        media = Media(
            result=make_result(provider="bandcamp"),
            streams=(
                Stream(id="mp3-128", ext="mp3", bitrate_kbps=128, codec="mp3"),
                Stream(id="flac", ext="flac", bitrate_kbps=None, codec="flac"),
            ),
            lossless=True,
        )
        model.apply_media(0, media)
        assert model.quality_label(0) == "FLAC"
        assert model.is_lossless(0)

    def test_applying_media_to_a_bad_row_is_a_no_op(self, qapp):
        model = results_ui.ResultModel()
        model.set_results([make_result()])
        model.apply_media(99, Media(result=make_result(), streams=()))
        assert model.rowCount() == 1

    def test_clearing_drops_probe_results_too(self, qapp):
        model = results_ui.ResultModel()
        model.set_results([make_result()])
        model.apply_media(0, Media(result=make_result(), streams=()))
        model.clear()
        model.set_results([make_result()])
        assert model.media_at(0) is None


# ---------------------------------------------------------------------------
# The job bridge
# ---------------------------------------------------------------------------


class TestJobBridge:
    def test_events_arrive_on_the_gui_thread(self, qapp):
        """Job callbacks fire on worker threads; widgets may only be touched
        from the GUI thread. The bridge is the only crossing."""
        queue = JobQueue(workers=2)
        bridge = JobBridge(queue)
        gui_thread = threading.current_thread()
        seen: list[threading.Thread] = []

        bridge.succeeded.connect(lambda snap: seen.append(threading.current_thread()))

        queue.submit(lambda ctx: "ok", kind="test")
        queue.wait_all(timeout=5)
        for _ in range(50):
            qapp.processEvents()
            if seen:
                break

        assert seen, "no succeeded signal arrived"
        assert all(t is gui_thread for t in seen)
        bridge.close()
        queue.shutdown()

    def test_failures_are_relayed(self, qapp):
        queue = JobQueue(workers=1)
        bridge = JobBridge(queue)
        failures = []
        bridge.failed.connect(failures.append)

        def boom(ctx):
            raise RuntimeError("nope")

        queue.submit(boom)
        queue.wait_all(timeout=5)
        for _ in range(50):
            qapp.processEvents()
            if failures:
                break
        assert failures and isinstance(failures[0].error, RuntimeError)
        bridge.close()
        queue.shutdown()

    def test_closing_stops_relaying(self, qapp):
        queue = JobQueue(workers=1)
        bridge = JobBridge(queue)
        events = []
        bridge.changed.connect(lambda e, s: events.append(e))
        bridge.close()

        queue.submit(lambda ctx: "ok")
        queue.wait_all(timeout=5)
        for _ in range(20):
            qapp.processEvents()
        assert events == []
        queue.shutdown()


# ---------------------------------------------------------------------------
# The activity strip
# ---------------------------------------------------------------------------


class TestActivityPanel:
    @pytest.fixture
    def panel(self, qapp):
        from neyta.ui.activity import ActivityPanel

        queue = JobQueue(workers=1)
        bridge = JobBridge(queue)
        panel = ActivityPanel(bridge)
        yield panel
        bridge.close()
        queue.shutdown()

    def drain(self, qapp, panel, until):
        for _ in range(100):
            qapp.processEvents()
            if until():
                return
        raise AssertionError("the panel never caught up")

    def overflowing(self, qapp, panel, lines=40):
        """A shown panel with more entries than fit, laid out.

        Qt grows the scrolled container on a posted layout request, so the
        range this asserts against only exists after the event loop has been
        round twice.
        """
        panel.resize(400, panel.height())
        panel.show()
        qapp.processEvents()
        for i in range(lines):
            panel.log(f"message {i}")
        bar = panel.scroll.verticalScrollBar()
        self.drain(qapp, panel, lambda: bar.maximum() > 0)
        return bar

    def test_it_starts_empty_and_says_so(self, panel):
        assert panel._empty.isVisibleTo(panel)

    def test_a_job_becomes_a_row(self, qapp, panel):
        panel.bridge.queue.submit(lambda ctx: "ok", kind="test", label="A job")
        panel.bridge.queue.wait_all(timeout=5)
        self.drain(qapp, panel, lambda: panel._rows)
        assert len(panel._rows) == 1

    def test_a_message_becomes_a_line(self, panel):
        panel.log("Saved A - Song.wav")
        assert panel._lines[-1].message == "Saved A - Song.wav"

    def test_a_line_is_stamped_with_the_time(self, panel):
        import re

        panel.log("something happened")
        assert re.match(r"^\d\d:\d\d:\d\d ", panel._lines[-1].full_text())

    def test_a_long_title_is_elided_rather_than_widening_the_row(self, qapp):
        from neyta.ui.widgets import ElidingLabel

        label = ElidingLabel("A - " + "very long title " * 12)
        label.resize(200, 20)
        assert label.text() != label.full_text()
        assert "…" in label.text()
        assert label.toolTip() == label.full_text()

    def test_a_blank_message_is_not_an_event(self, panel):
        # The status bar clearing itself is not something that happened.
        panel.log("")
        panel.log("   ")
        assert panel._lines == []

    def test_the_same_message_twice_running_is_said_once(self, panel):
        panel.log("Searching YouTube…")
        panel.log("Searching YouTube…")
        assert len(panel._lines) == 1

    def test_the_queue_and_the_log_share_one_list(self, qapp, panel):
        panel.bridge.queue.submit(lambda ctx: "ok", kind="test", label="A job")
        panel.bridge.queue.wait_all(timeout=5)
        self.drain(qapp, panel, lambda: panel._rows)
        panel.log("and then this happened")
        row = next(iter(panel._rows.values()))
        assert panel._list.indexOf(row) < panel._list.indexOf(panel._lines[-1])

    def test_nothing_has_to_be_cleared_by_hand(self, panel):
        from PySide6.QtWidgets import QPushButton

        assert [b.text() for b in panel.findChildren(QPushButton)] == []

    def test_the_oldest_finished_entries_fall_off(self, panel):
        from neyta.ui.activity import MAX_ENTRIES

        for i in range(MAX_ENTRIES + 10):
            panel.log(f"message {i}")
        assert len(panel._entries) == MAX_ENTRIES
        assert panel._lines[0].message == "message 10"
        assert panel._lines[-1].message == f"message {MAX_ENTRIES + 9}"

    def test_a_running_job_is_never_trimmed_away(self, qapp, panel):
        from neyta.ui.activity import MAX_ENTRIES

        release = threading.Event()
        panel.bridge.queue.submit(
            lambda ctx: release.wait(10), kind="test", label="Slow"
        )
        try:
            self.drain(qapp, panel, lambda: panel._rows)
            for i in range(MAX_ENTRIES + 10):
                panel.log(f"message {i}")
            assert len(panel._rows) == 1, "the job you are waiting on stayed"
        finally:
            release.set()
            panel.bridge.queue.wait_all(timeout=10)

    def test_the_newest_entry_is_the_one_on_screen(self, qapp, panel):
        bar = self.overflowing(qapp, panel)
        assert bar.value() == bar.maximum()
        panel.close()

    def test_it_jumps_back_down_when_something_new_arrives(self, qapp, panel):
        bar = self.overflowing(qapp, panel)
        bar.setValue(0)  # scrolled up, reading back
        panel.log("and then this happened")
        self.drain(qapp, panel, lambda: bar.value() == bar.maximum())
        assert bar.value() == bar.maximum()
        panel.close()

    def test_a_new_job_scrolls_into_view_too(self, qapp, panel):
        bar = self.overflowing(qapp, panel)
        bar.setValue(0)
        panel.bridge.queue.submit(lambda ctx: "ok", kind="test", label="A job")
        panel.bridge.queue.wait_all(timeout=5)
        self.drain(qapp, panel,
                   lambda: panel._rows and bar.value() == bar.maximum())
        assert bar.value() == bar.maximum()
        panel.close()

    def test_rows_alternate_the_two_list_colours(self, panel):
        base, alternate = panel.stripe_colours()
        assert base != alternate
        for i in range(4):
            panel.log(f"message {i}")
        stripes = [e.stripe for e in panel._entries]
        assert stripes == [base, alternate, base, alternate]

    def test_the_banding_is_the_palette_the_other_lists_use(self, panel):
        from PySide6.QtGui import QPalette

        palette = panel.palette()
        assert panel.stripe_colours() == (
            palette.color(QPalette.Base), palette.color(QPalette.AlternateBase),
        )

    def test_jobs_are_banded_alongside_messages(self, qapp, panel):
        panel.log("before")
        panel.bridge.queue.submit(lambda ctx: "ok", kind="test", label="A job")
        panel.bridge.queue.wait_all(timeout=5)
        self.drain(qapp, panel, lambda: panel._rows)
        row = next(iter(panel._rows.values()))
        assert row.stripe == panel.stripe_colours()[1]

    def test_trimming_re_bands_what_is_left(self, panel):
        from neyta.ui.activity import MAX_ENTRIES

        # Dropping an odd number off the top would otherwise leave two rows
        # of the same shade sitting next to each other.
        for i in range(MAX_ENTRIES + 3):
            panel.log(f"message {i}")
        base, alternate = panel.stripe_colours()
        stripes = [e.stripe for e in panel._entries]
        assert stripes[::2] == [base] * (len(stripes) // 2)
        assert stripes[1::2] == [alternate] * (len(stripes) // 2)

    def test_one_row_is_the_whole_strip(self, panel):
        from neyta.ui.activity import DEFAULT_ROWS

        assert DEFAULT_ROWS == 1
        assert panel.sizeHint().height() == panel.preferred_height(rows=1)
        assert panel.height() == panel.preferred_height()

    def test_the_height_is_fixed(self, panel):
        assert panel.minimumHeight() == panel.maximumHeight()

    def test_it_carries_no_scrollbars(self, panel):
        from PySide6.QtCore import Qt

        assert panel.scroll.verticalScrollBarPolicy() == Qt.ScrollBarAlwaysOff
        assert panel.scroll.horizontalScrollBarPolicy() == Qt.ScrollBarAlwaysOff


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------


@pytest.fixture
def window(qapp, tmp_path):
    from neyta.app import Services
    from neyta.ui.window import MainWindow

    services = Services(config.Paths.under(tmp_path), native=False)
    win = MainWindow(services)
    try:
        yield win
    finally:
        win.bridge.close()
        services.shutdown()


class TestGroups:
    def test_startup_does_not_read_paid_engine_keys(self, qapp, tmp_path):
        from neyta.app import Services
        from neyta.settings import FakeKeyring, SecretStore
        from neyta.ui.window import MainWindow

        class CountingKeyring(FakeKeyring):
            def __init__(self):
                super().__init__()
                self.reads = []

            def get_password(self, service, username):
                self.reads.append((service, username))
                return super().get_password(service, username)

        services = Services(config.Paths.under(tmp_path), native=False)
        backend = CountingKeyring()
        services.settings.secrets = SecretStore(backend=backend)
        services.settings.phrase_engine = "filmot"
        services.settings.stem_engine = "lalal"

        win = MainWindow(services)
        try:
            assert backend.reads == []
        finally:
            win.bridge.close()
            services.shutdown()

    def test_group_one_is_a_tab_per_source(self, window):
        labels = [window.tabs.tabText(i) for i in range(window.tabs.count())]
        assert labels == [
            "YouTube", "SoundCloud", "Bandcamp", "Soulseek", "Spotify",
        ]

    def test_group_two_is_a_bar_of_its_own(self, window):
        # Off at the other end of the row, not one more place to search.
        from neyta.ui.window import TABS, PAGE_DOWNLOADED, PAGE_SEARCH

        assert list(window.page_bar.buttons) == [PAGE_SEARCH, PAGE_DOWNLOADED]
        assert window.tabs.count() == len(TABS)

    def test_the_sources_sit_on_the_centre_line(self, window):
        header = window.header_row
        # Equal weight either side is what puts the sources on the window's
        # centre line rather than on what is left over.
        assert cell(header, window.tabs) == (0, 1)
        assert header.columnStretch(0) == header.columnStretch(2) > 0
        assert header.columnStretch(1) == 0

    def test_the_pages_are_top_left_and_the_gear_top_right(self, window):
        header = window.header_row
        assert cell(header, window.page_bar) == (0, 0)
        assert cell(header, window.settings_button) == (0, 2)

    def test_each_page_is_an_icon(self, window):
        from neyta.ui.pagebar import ICON_FOLDER, ICON_SEARCH
        from neyta.ui.window import PAGE_DOWNLOADED, PAGE_SEARCH

        assert window.page_bar.buttons[PAGE_SEARCH]._icon == ICON_SEARCH
        assert window.page_bar.buttons[PAGE_DOWNLOADED]._icon == ICON_FOLDER

    def test_an_icon_still_says_its_name_out_loud(self, window):
        # A picture of a folder is not readable by a screen reader, and is not
        # readable by anyone who has not met this one yet.
        from neyta.ui.window import PAGE_DOWNLOADED, PAGE_SEARCH

        assert window.page_bar.buttons[PAGE_SEARCH].toolTip() == "Search"
        folder = window.page_bar.buttons[PAGE_DOWNLOADED]
        assert folder.toolTip() == "Downloaded"
        assert folder.accessibleName() == "Downloaded"

    def test_each_tab_is_highlighted_in_its_service_colour(self, window):
        from neyta.ui.window import DOWNLOADED_COLOUR, TABS

        for index, (key, _) in enumerate(TABS):
            assert window.tabs.tab_colour(index).name() == {
                "youtube": "#e62117", "soundcloud": "#ff5500",
                "bandcamp": "#6e7378", "soulseek": "#2f6fd0",
                "spotify": "#12a150",
            }[key]
        assert window.page_bar.buttons[
            "downloaded"].colour().name() == DOWNLOADED_COLOUR

    def test_the_colour_is_the_highlight_not_the_lettering(self, window):
        # A bar of coloured words is harder to read than a bar of plain ones.
        assert window.tabs.tabTextColor(0).name() == "#000000"

    def test_downloaded_is_the_green_the_activity_strip_uses(self, window):
        from neyta.ui.activity import SUCCESS_COLOUR
        from neyta.ui.window import PAGE_DOWNLOADED

        assert window.page_bar.buttons[
            PAGE_DOWNLOADED].colour().name() == SUCCESS_COLOUR

    def test_it_opens_on_group_one(self, window):
        from neyta.ui.window import GROUP_SOURCES

        assert window.group == GROUP_SOURCES
        assert window.pages.currentWidget() is window.search_page

    def test_group_two_takes_over_the_window(self, window):
        from neyta.ui.window import GROUP_DOWNLOADED

        window.select_group(GROUP_DOWNLOADED)
        assert window.group == GROUP_DOWNLOADED
        assert window.pages.currentWidget() is window.tray
        assert not window.search_page.isVisibleTo(window.pages)

    def test_switching_groups_leaves_the_search_alone(self, window):
        # The two groups are different activities; visiting one must not
        # throw away what you were doing in the other.
        from neyta.ui.window import GROUP_DOWNLOADED

        window.model.set_results([make_result(), make_result()])
        window.select_group(GROUP_DOWNLOADED)
        window.tabs.tabBarClicked.emit(0)
        assert window.model.rowCount() == 2
        assert window.pages.currentWidget() is window.search_page

    def test_group_two_borrows_the_source_you_left(self, window):
        from neyta.ui.window import GROUP_DOWNLOADED

        window.tabs.setCurrentIndex(2)  # Bandcamp
        window.select_group(GROUP_DOWNLOADED)
        assert window.tab_key == "bandcamp"
        assert window.provider.key == "bandcamp"

    def test_the_activity_strip_belongs_to_neither_group(self, window):
        # A download queued in group 1 is still running while you are in
        # group 2, so its row has to stay visible.
        from neyta.ui.window import GROUP_DOWNLOADED

        window.select_group(GROUP_DOWNLOADED)
        assert window.activity.isVisibleTo(window)

    def test_the_downloaded_page_starts_from_what_is_on_disk(
        self, qapp, tmp_path
    ):
        from neyta.app import Services
        from neyta.ui.window import MainWindow

        folder = tmp_path / "Music"
        folder.mkdir()
        (folder / "already here.wav").write_bytes(b"RIFF")
        services = Services(config.Paths.under(tmp_path), native=False)
        services.settings.download_dir = folder
        win = MainWindow(services)
        try:
            assert [p.name for p in win.tray.paths()] == ["already here.wav"]
            assert win.tray.path_field.text() == str(folder)
        finally:
            win.bridge.close()
            services.shutdown()

    def test_the_path_readout_follows_the_download_folder(self, window, tmp_path):
        window.settings.download_dir = tmp_path
        window._update_folder_label()
        assert window.tray.path_field.text() == str(tmp_path)

    def test_the_badge_counts_arrivals_not_files_in_the_folder(
        self, window, audio_files
    ):
        # Listing a folder full of old downloads is not news.
        window.tray.add(audio_files[0])
        window.tray.add(audio_files[1])
        assert badge(window) == 0

        window.note_arrival()
        assert badge(window) == 1
        window.note_arrival(3)
        assert badge(window) == 4

    def test_the_badge_says_what_it_is_counting(self, window):
        from neyta.ui.window import PAGE_DOWNLOADED

        window.note_arrival(2)
        folder = window.page_bar.buttons[PAGE_DOWNLOADED]
        assert folder.toolTip() == "Downloaded (2 new)"
        # Past a handful the number stops being a count and starts being
        # "several" — and stops fitting in the dot.
        window.note_arrival(20)
        assert folder.badge_text() == "9+"

    def test_reading_the_page_clears_the_badge(self, window):
        from neyta.ui.window import GROUP_DOWNLOADED, GROUP_SOURCES

        window.note_arrival(2)
        window.select_group(GROUP_DOWNLOADED)
        window.select_group(GROUP_SOURCES)
        assert badge(window) == 0

    def test_nothing_is_badged_while_you_are_looking_at_it(self, window):
        from neyta.ui.window import GROUP_DOWNLOADED

        window.select_group(GROUP_DOWNLOADED)
        window.note_arrival()
        assert badge(window) == 0
        window.tabs.tabBarClicked.emit(0)
        assert badge(window) == 0

    def test_deleting_takes_the_file_off_the_disk(self, window, audio_files,
                                                  monkeypatch, tmp_path):
        bin_dir = tmp_path / "Trash"
        monkeypatch.setattr("neyta.core.trash.trash_dir", lambda home=None: bin_dir)
        window.tray.add(audio_files[0])
        window.delete_files([audio_files[0]])
        assert not audio_files[0].exists()
        assert (bin_dir / audio_files[0].name).exists(), "recoverable, not shredded"
        assert window.tray.paths() == []

    def test_deleting_says_what_it_did(self, window, audio_files, monkeypatch,
                                       tmp_path):
        monkeypatch.setattr("neyta.core.trash.trash_dir",
                            lambda home=None: tmp_path / "Trash")
        window.delete_files(audio_files[:2])
        assert "2 files moved to the Trash" in window.statusBar().currentMessage()

    def test_a_stale_row_is_dropped_rather_than_erroring(self, window, tmp_path):
        window.delete_files([tmp_path / "never existed.wav"])
        assert window.tray.paths() == []

    def test_nothing_is_deleted_permanently_without_asking(
        self, window, audio_files, monkeypatch
    ):
        from neyta.core import trash

        def no_trash(path, **kw):
            raise trash.TrashUnavailable("no bin")

        monkeypatch.setattr(trash, "move_to_trash", no_trash)
        monkeypatch.setattr(window, "_confirm_permanent_delete", lambda paths: False)
        window.delete_files([audio_files[0]])
        assert audio_files[0].exists()

    def test_a_confirmed_permanent_delete_goes_through(
        self, window, audio_files, monkeypatch
    ):
        from neyta.core import trash

        def no_trash(path, **kw):
            raise trash.TrashUnavailable("no bin")

        monkeypatch.setattr(trash, "move_to_trash", no_trash)
        monkeypatch.setattr(window, "_confirm_permanent_delete", lambda paths: True)
        window.delete_files([audio_files[0]])
        assert not audio_files[0].exists()

    def test_a_finished_download_badges_the_switcher(self, window, audio_files):
        from neyta.core.jobs import JobSnapshot, JobState

        window._on_job_succeeded(JobSnapshot(
            id=1, kind="download", label="x", state=JobState.SUCCEEDED,
            progress=1.0, message="", attempt=1, max_retries=0,
            result=audio_files[0],
        ))
        assert badge(window) == 1

    def test_a_finished_separation_badges_every_stem(self, window, audio_files):
        from neyta.core.jobs import JobSnapshot, JobState

        window._on_job_succeeded(JobSnapshot(
            id=2, kind="stems", label="x", state=JobState.SUCCEEDED,
            progress=1.0, message="", attempt=1, max_retries=0,
            result={"vocals": audio_files[0], "drums": audio_files[1]},
        ))
        assert badge(window) == 2

    def test_the_page_you_are_on_is_the_one_filled_in(self, window):
        from neyta.ui.window import (
            GROUP_DOWNLOADED, GROUP_SOURCES, PAGE_DOWNLOADED, PAGE_SEARCH,
        )

        assert window.page_bar.current() == PAGE_SEARCH
        window.select_group(GROUP_DOWNLOADED)
        assert window.page_bar.current() == PAGE_DOWNLOADED
        window.select_group(GROUP_SOURCES)
        assert window.page_bar.current() == PAGE_SEARCH

    def test_pressing_an_icon_does_not_light_it_on_its_own(self, window):
        # The window says which page is on screen; the button obeys, so a page
        # that refuses to open cannot leave its icon filled in.
        from neyta.ui.window import PAGE_DOWNLOADED, PAGE_SEARCH

        folder = window.page_bar.buttons[PAGE_DOWNLOADED]
        folder.nextCheckState()
        assert not folder.isChecked()
        assert window.page_bar.current() == PAGE_SEARCH

    def test_the_magnifier_wears_the_colour_of_the_source_it_returns_to(
        self, window
    ):
        from neyta.ui.window import PAGE_SEARCH

        search = window.page_bar.buttons[PAGE_SEARCH]
        assert search.colour().name() == "#e62117"   # YouTube
        window.tabs.setCurrentIndex(1)
        assert search.colour().name() == "#ff5500"   # SoundCloud

    def test_one_press_goes_to_a_page_and_the_other_comes_back(self, window):
        from neyta.ui.window import (
            GROUP_DOWNLOADED, GROUP_SOURCES, PAGE_DOWNLOADED, PAGE_SEARCH,
        )

        window.page_bar.selected.emit(PAGE_DOWNLOADED)
        assert window.group == GROUP_DOWNLOADED
        window.page_bar.selected.emit(PAGE_SEARCH)
        assert window.group == GROUP_SOURCES

    def test_settings_opens_as_a_modeless_window(self, window):
        group = window.group
        page = window.pages.currentWidget()
        window.open_settings()

        assert window.settings_dialog.isWindow()
        assert window.settings_dialog.isVisible()
        assert not window.settings_dialog.isModal()
        assert window.group == group
        assert window.pages.currentWidget() is page
        assert window.activity.isVisibleTo(window)

    def test_settings_does_not_change_page_selection(self, window):
        window.open_settings()
        assert not window.settings_button.isChecked()
        assert window.page_bar.current() == "search"

    def test_the_sources_remain_usable_behind_settings(self, window):
        window.open_settings()
        assert window.tabs.isVisibleTo(window)
        assert window.search.isEnabled()

    def test_closing_settings_saves_them(self, window, tmp_path):
        target = tmp_path / "Elsewhere"
        target.mkdir()
        window.open_settings()
        window.settings_page.downloads.setText(str(target))
        window.settings_page.done_button.click()
        assert not window.settings_dialog.isVisible()
        assert Path(window.settings.download_dir) == target

    def test_arriving_re_reads_what_is_true_now(self, window, tmp_path):
        # The folder can be changed from the downloaded page while settings
        # is off screen.
        window.open_settings()
        window.settings_dialog.close()
        target = tmp_path / "Later"
        target.mkdir()
        window.settings.download_dir = target
        window.open_settings()
        assert window.settings_page.downloads.text() == str(target)

    def test_saving_rebuilds_the_engines_that_carry_credentials(self, window):
        before = window.providers["youtube"].engine
        window.settings_page.saved.emit()
        assert window.providers["youtube"].engine is not before

    def test_pressing_the_page_you_are_on_leaves_you_there(self, window):
        # An icon is a place, not a door: it cannot toggle you off itself.
        from neyta.ui.window import GROUP_SOURCES, PAGE_SEARCH

        window.page_bar.selected.emit(PAGE_SEARCH)
        window.page_bar.selected.emit(PAGE_SEARCH)
        assert window.group == GROUP_SOURCES
        assert window.page_bar.current() == PAGE_SEARCH

    def test_the_sources_are_gone_while_you_are_on_the_downloaded_page(
        self, window
    ):
        from neyta.ui.window import GROUP_DOWNLOADED, GROUP_SOURCES

        window.select_group(GROUP_DOWNLOADED)
        assert not window.tabs.isVisibleTo(window)
        window.select_group(GROUP_SOURCES)
        assert window.tabs.isVisibleTo(window)


class TestTheSpotifyTab:
    def test_it_is_one_of_the_sources(self, window):
        from neyta.providers.lucida import LucidaProvider

        assert isinstance(window.providers["spotify"], LucidaProvider)

    def test_its_server_is_not_started_by_opening_the_app(self, window):
        # Same rule as slskd: a tab nobody pressed starts nothing, and this
        # one would start a headless browser.
        assert not window.lucida.is_running()

    def test_searching_without_the_checkout_says_where_to_look(self, window):
        from neyta.ui.window import GROUP_SOURCES, TABS

        if window.lucida.installed():
            pytest.skip("lucida-flow is installed on this machine")
        window.tabs.setCurrentIndex(
            next(i for i, (k, _) in enumerate(TABS) if k == "spotify")
        )
        window.search.setText("hotel california")
        window.run_search()
        assert "lucida-flow" in window.statusBar().currentMessage()
        # ...and opens the popup that explains it without leaving search.
        assert window.settings_dialog.isVisible()
        assert window.group == GROUP_SOURCES

    def test_the_tab_carries_its_own_slow_search_warning(self, window):
        from neyta.ui.window import SEARCH_NOTE

        assert "browser" in SEARCH_NOTE["spotify"]


class TestStemEngineInTheWindow:
    def test_it_starts_on_the_local_engine(self, window):
        from neyta.core.stems import StemSeparator

        assert isinstance(window.separator, StemSeparator)

    def test_saving_settings_keeps_paid_credentials_lazy(self, window):
        from neyta.core.stems import StemSeparator

        window.settings.stem_engine = "lalal"
        window.settings.set_credential("lalal", "api_key", "licence")
        window._on_settings_saved()
        assert isinstance(window.separator, StemSeparator)

    def test_the_reason_it_cannot_run_belongs_to_the_engine(
        self, window, monkeypatch
    ):
        from neyta.core.lalal import LalalSeparator
        from neyta.core import stems

        monkeypatch.setattr(
            stems, "separator_for",
            lambda settings, calibration: LalalSeparator(api_key=""),
        )
        window._queue_separation(
            Path("nothing.wav"), make_result(),
            _choice_wanting_stems(window),
        )
        assert "licence key" in window.statusBar().currentMessage()


def _choice_wanting_stems(window):
    from neyta.ui.exportdialog import ExportChoice

    return ExportChoice(
        fmt=config.WAV_48_24, stems=["vocals"],
        dest_dir=Path(window.settings.download_dir),
    )


class TestWindow:
    def test_every_tab_is_selectable(self, window):
        assert all(window.tabs.isTabEnabled(i)
                   for i in range(window.tabs.count()))

    def test_the_daemon_is_not_started_just_by_opening_the_app(self, window):
        # Starting slskd logs into Soulseek, which knocks the user's desktop
        # client offline. That happens when they ask for it, not because the
        # window opened.
        assert not window.slskd.is_running()

    def test_switching_tabs_swaps_the_provider(self, window):
        window.tabs.setCurrentIndex(0)
        assert window.provider.key == "youtube"
        window.tabs.setCurrentIndex(2)
        assert window.provider.key == "bandcamp"

    def test_youtube_has_no_audio_ceiling_warning(self, window):
        window.tabs.setCurrentIndex(0)
        assert window.ceiling.text() == ""

    def test_other_source_notes_still_follow_the_tab(self, window):
        window.tabs.setCurrentIndex(1)
        assert "160k AAC" in window.ceiling.text()

    def test_the_last_tab_is_persisted(self, window):
        window.tabs.setCurrentIndex(2)
        assert window.settings.get("ui/last_tab") == "bandcamp"

    def test_switching_tabs_clears_stale_results(self, window):
        window.model.set_results([make_result()])
        window.tabs.setCurrentIndex(1)
        assert window.model.rowCount() == 0

    def test_download_is_disabled_with_no_selection(self, window):
        assert not window.download_button.isEnabled()

    def test_an_empty_search_does_nothing(self, window):
        window.search.setText("   ")
        window.run_search()
        assert not window.services.queue.jobs()

    def test_the_crate_panel_is_youtube_only(self, window):
        attach_shuffle_library(window)
        window.tabs.setCurrentIndex(0)
        assert window.shuffle_button.isVisibleTo(window)
        assert window.shuffle_settings_button.isVisibleTo(window)
        assert not window.shuffle_popup.isVisible()
        window.tabs.setCurrentIndex(2)
        assert not window.shuffle_button.isVisibleTo(window)
        assert not window.shuffle_settings_button.isVisibleTo(window)
        assert not window.shuffle_popup.isVisible()

    def test_shuffle_is_present_before_the_local_library_exists(self, window):
        window.shuffle_panel.attach(None)
        window._refresh_shuffle_controls()
        window.tabs.setCurrentIndex(0)
        assert window.shuffle_panel.library is None
        assert window.shuffle_button.isVisibleTo(window)
        assert window.shuffle_button.isEnabled()
        assert not window.shuffle_settings_button.isEnabled()

    def test_first_shuffle_starts_the_local_library(self, window, monkeypatch):
        window.shuffle_panel.attach(None)
        window._refresh_shuffle_controls()
        started = []
        monkeypatch.setattr(
            window, "_start_samplette_library", lambda: started.append(True)
        )
        window.tabs.setCurrentIndex(0)
        window.shuffle_button.click()
        assert started == [True]

    def test_the_library_attaches_as_soon_as_tracks_are_ready(
        self, window, monkeypatch
    ):
        from neyta.core import samplette

        monkeypatch.setattr(samplette, "SampletteLibrary", FakeShuffleLibrary)
        window._refresh_samplette_library()

        assert isinstance(window.shuffle_panel.library, FakeShuffleLibrary)
        assert window.shuffle_button.isEnabled()
        assert window.shuffle_settings_button.isEnabled()

    def test_the_shuffle_settings_link_toggles_the_popup(self, window):
        attach_shuffle_library(window)
        window.tabs.setCurrentIndex(0)
        window.shuffle_settings_button.click()
        assert window.shuffle_popup.isVisible()
        window.shuffle_settings_button.click()
        assert not window.shuffle_popup.isVisible()

    def test_tempo_and_year_are_single_boxes(self, window):
        from PySide6.QtWidgets import QLineEdit

        assert isinstance(window.shuffle_panel.tempo, QLineEdit)
        assert isinstance(window.shuffle_panel.year, QLineEdit)

    def test_the_single_boxes_still_parse_ranges(self, window):
        from neyta.core import samplette as S

        window.shuffle_panel.tempo.setText("90-110")
        window.shuffle_panel.year.setText("1970-1979")
        filters = window.shuffle_panel.filters()
        assert filters.tempo == S.Range(90, 110)
        assert filters.year == S.Range(1970, 1979)

    def test_one_number_means_an_exact_tempo_or_year(self, window):
        from neyta.core import samplette as S

        window.shuffle_panel.tempo.setText("98")
        window.shuffle_panel.year.setText("1978")
        filters = window.shuffle_panel.filters()
        assert filters.tempo == S.Range(98, 98)
        assert filters.year == S.Range(1978, 1978)

    def test_bad_range_text_disables_shuffle_and_explains_why(self, window):
        attach_shuffle_library(window)
        window.shuffle_panel.tempo.setText("fast")
        assert "tempo must look like" in window.shuffle_panel.matches.text()
        assert not window.shuffle_button.isEnabled()

    def test_nothing_asks_for_a_format_until_a_file_is_asked_for(self, window):
        # The export question belongs to the moment of downloading, not to
        # the moment of opening the app.
        assert not hasattr(window, "format_box")
        assert not hasattr(window, "stem_picker")

    def test_the_search_button_sits_under_the_field(self, window):
        rows = window._left_layout
        assert rows.indexOf(window.button_row) == rows.indexOf(window.search_row) + 1

    def test_both_are_centred_with_the_field_the_wider_one(self, window):
        from neyta.ui.widgets import FIELD_BUTTON_WIDTH, FIELD_WIDTH

        # A stretch on either side of each is what centres them.
        for row, widget in ((window.search_row, window.search),
                            (window.button_row, window.search_controls)):
            assert row.indexOf(widget) == 1 and row.count() == 3
        assert window.search.maximumWidth() == FIELD_WIDTH
        assert window.search_button.width() == FIELD_BUTTON_WIDTH
        assert window.shuffle_button.width() == FIELD_BUTTON_WIDTH
        assert FIELD_BUTTON_WIDTH < FIELD_WIDTH < window.width()

    def test_the_option_rows_sit_together_under_the_search_box(self, window):
        rows = window._left_layout
        assert rows.indexOf(window.phrase_panel) == rows.indexOf(window.button_row) + 1
        # ...and the popup itself stays out of the page layout.
        assert rows.indexOf(window.table) == rows.indexOf(window.phrase_panel) + 1
        assert window.shuffle_panel.parent() is window.shuffle_popup

    def test_the_shuffle_panel_lives_in_a_popup_not_the_page(self, window):
        from PySide6.QtCore import Qt

        assert window._left_layout.indexOf(window.shuffle_panel) == -1
        assert window.shuffle_popup.windowFlags() & Qt.Popup

    def test_the_player_bar_sits_below_the_pages(self, window):
        root = window.root_layout
        assert root.indexOf(window.download_bar) == root.indexOf(window.pages) + 1
        assert root.indexOf(window.activity) == root.indexOf(window.download_bar) + 1
        assert window._left_layout.indexOf(window.download_bar) == -1

    def test_the_bar_stays_away_until_there_is_something_to_download(
        self, window
    ):
        assert not window.download_bar.isVisibleTo(window)
        window.set_download_available(True)
        assert window.download_bar.isVisibleTo(window)
        window.set_download_available(False)
        assert not window.download_bar.isVisibleTo(window)

    def test_a_running_download_shows_a_progress_bar_in_the_bar(self, window):
        from neyta.core.jobs import JobSnapshot, JobState

        snapshot = JobSnapshot(
            id=1, kind="download", label="wav_48_24: A Track",
            state=JobState.RUNNING, progress=0.42, message="downloading",
            attempt=1, max_retries=0,
        )
        window._on_job_progress("progress", snapshot)
        assert window.download_bar.working
        assert window.download_bar.progress.value() == 420
        assert "A Track" in window.download_bar.progress_label.full_text()
        assert window.download_bar.state.text() == "DOWNLOADING"
        # The bar earns its place while working, even with nothing selected.
        assert window.download_bar.isVisibleTo(window)

    def test_the_progress_gives_way_to_the_destination_when_it_finishes(
        self, window
    ):
        from neyta.core.jobs import JobSnapshot, JobState

        running = JobSnapshot(
            id=1, kind="download", label="x", state=JobState.RUNNING,
            progress=0.5, message="", attempt=1, max_retries=0,
        )
        window._on_job_progress("progress", running)
        done = JobSnapshot(
            id=1, kind="download", label="x", state=JobState.SUCCEEDED,
            progress=1.0, message="", attempt=1, max_retries=0,
        )
        window._on_job_progress("finished", done)
        assert not window.download_bar.working

    def test_a_separation_keeps_the_bar_it_was_asked_from(self, window):
        from neyta.core.jobs import JobSnapshot, JobState

        window._on_job_progress("progress", JobSnapshot(
            id=2, kind="stems", label="Stems: A Track", state=JobState.RUNNING,
            progress=0.1, message="separating", attempt=1, max_retries=0,
        ))
        assert window.download_bar.working

    def test_a_search_job_is_not_a_transfer(self, window):
        from neyta.core.jobs import JobSnapshot, JobState

        window._on_job_progress("progress", JobSnapshot(
            id=3, kind="search", label="Search YouTube", state=JobState.RUNNING,
            progress=0.1, message="", attempt=1, max_retries=0,
        ))
        assert not window.download_bar.working

    def test_the_player_bar_has_track_transport_and_actions(self, window):
        row = window.controls_row
        bar = window.download_bar
        assert cell(row, bar.track_widget) == (0, 0)
        assert cell(row, bar.transport_widget) == (0, 1)
        assert cell(row, bar.actions_widget) == (0, 2)

    def test_the_transport_looks_like_a_media_player(self, window):
        bar = window.download_bar
        assert not bar.preview_button.icon().isNull()
        assert bar.timeline.orientation() == Qt.Horizontal
        assert bar.elapsed.text() == "0:00"
        assert bar.duration_label.text() == "0:00"

    def test_volume_and_download_are_in_the_action_cluster(self, window):
        bar = window.download_bar
        assert bar.actions_layout.indexOf(bar.volume) >= 0
        assert bar.actions_layout.indexOf(window.download_button) >= 0

    def test_the_gear_stays_reachable_from_group_two(self, window):
        from neyta.ui.window import GROUP_DOWNLOADED

        window.select_group(GROUP_DOWNLOADED)
        assert window.settings_button.isVisibleTo(window)
        # ...but nothing about a result list is.
        assert not window.download_button.isVisibleTo(window)
        assert not window.ceiling.isVisibleTo(window)

    def test_settings_is_a_gear_in_the_corner(self, window):
        from neyta.ui.pagebar import ICON_GEAR

        assert window.settings_button._icon == ICON_GEAR
        # The icon carries no meaning to a screen reader on its own.
        assert window.settings_button.accessibleName() == "Settings"
        assert "Settings" in window.settings_button.toolTip()

    def test_the_gear_is_top_right(self, window):
        assert cell(window.header_row, window.settings_button) == (0, 2)
        assert window.controls_row.indexOf(window.settings_button) == -1

    def test_the_gear_is_the_same_button_as_the_other_pages(self, window):
        # Three pages, three icons: one of them looking like a different kind
        # of control implies it does a different kind of thing.
        from neyta.ui.pagebar import PageButton
        from neyta.ui.window import PAGE_SEARCH

        assert isinstance(window.settings_button, PageButton)
        assert window.settings_button.size() == \
            window.page_bar.buttons[PAGE_SEARCH].size()
        assert window.settings_button.height() > window.tabs.sizeHint().height()

    def test_the_player_is_not_built_until_it_is_asked_for(self, window):
        # QtWebEngine is the 332 MB half of PySide6-Addons.
        assert window.preview._view is None
        assert not window.download_bar.player.isVisible()

    def test_selecting_a_row_does_not_open_the_player(self, window):
        window.model.set_results([make_result()])
        window.model.apply_media(0, aac_media())
        window.table.selectRow(0)
        assert window.preview._view is None
        assert not window.download_bar.player.isVisible()

    def test_preview_with_nothing_chosen_says_so(self, window):
        window.tray.preview_requested.emit()
        assert "preview" in window.statusBar().currentMessage()
        assert not window.download_bar.player.isVisible()

    def test_the_preview_button_text_follows_the_tab(self, window):
        assert window.preview_button.accessibleName() == "Preview"
        soulseek = next(
            i for i in range(window.tabs.count())
            if window.tabs.tabText(i) == "Soulseek"
        )
        window.tabs.setCurrentIndex(soulseek)
        assert window.preview_button.accessibleName() == "Fetch & preview"

    def test_clicking_preview_uses_the_current_result(self, window, monkeypatch):
        window.model.set_results([make_result(id="row-1")])
        window.table.selectRow(0)
        seen = []
        monkeypatch.setattr(window, "_show_preview", lambda result: seen.append(result.id))
        window.preview_button.click()
        assert seen == ["row-1"]

    def test_double_clicking_a_row_previews_that_result(self, window, monkeypatch):
        window.model.set_results([make_result(id="row-1"), make_result(id="row-2")])
        seen = []
        monkeypatch.setattr(window, "_show_preview", lambda result: seen.append(result.id))
        window.table.doubleClicked.emit(window.model.index(1, 0))
        assert seen == ["row-2"]

    def test_double_click_sets_the_mini_player_to_streaming(
        self, window, monkeypatch
    ):
        from neyta.providers.base import Embed

        window.model.set_results([make_result(id="row-1")])
        window.table.selectRow(0)
        monkeypatch.setattr(
            window.provider, "preview",
            lambda _result: Embed("https://example.test/embed?autoplay=1"),
        )
        shown = []
        monkeypatch.setattr(
            window.preview, "show_preview", lambda preview: shown.append(preview)
        )
        window.table.doubleClicked.emit(window.model.index(0, 0))
        assert shown
        assert window.download_bar.isVisibleTo(window)
        assert window.download_bar.state.text() == "STREAMING"

    def test_selected_track_populates_title_artist_and_timeline(self, window):
        window.model.set_results([
            make_result(title="Track Name", artist="Artist Name", duration=125)
        ])
        window.table.selectRow(0)
        bar = window.download_bar
        assert bar.title.full_text() == "Track Name"
        assert bar.artist.full_text() == "Artist Name"
        assert bar.timeline.maximum() == 125_000
        assert bar.duration_label.text() == "2:05"

    def test_local_player_position_moves_the_timeline(self, window, monkeypatch):
        from neyta.providers.base import LocalFile

        bar = window.download_bar
        monkeypatch.setattr(bar.player, "show_preview", lambda _preview: None)
        bar.show_preview(LocalFile(Path("/tmp/track.wav")), "Track")
        bar.player.duration_changed.emit(185_000)
        bar.player.position_changed.emit(65_000)
        assert bar.timeline.maximum() == 185_000
        assert bar.timeline.value() == 65_000
        assert bar.elapsed.text() == "1:05"
        assert bar.duration_label.text() == "3:05"

    def test_releasing_the_timeline_seeks_local_audio(self, window, monkeypatch):
        from neyta.providers.base import LocalFile

        bar = window.download_bar
        monkeypatch.setattr(bar.player, "show_preview", lambda _preview: None)
        sought = []
        monkeypatch.setattr(bar.player, "seek", sought.append)
        bar.show_preview(LocalFile(Path("/tmp/track.wav")), "Track", duration=200)
        bar.timeline.setValue(90_000)
        bar.timeline.sliderReleased.emit()
        assert sought == [90_000]

    def test_play_button_toggles_local_audio(self, window, monkeypatch):
        from neyta.providers.base import LocalFile

        bar = window.download_bar
        monkeypatch.setattr(bar.player, "show_preview", lambda _preview: None)
        toggled = []
        monkeypatch.setattr(bar.player, "toggle_playback", lambda: toggled.append(True))
        bar.show_preview(LocalFile(Path("/tmp/track.wav")), "Track")
        bar.preview_button.click()
        assert toggled == [True]

    def test_local_playback_state_updates_button_and_status(self, window, monkeypatch):
        from neyta.providers.base import LocalFile

        bar = window.download_bar
        monkeypatch.setattr(bar.player, "show_preview", lambda _preview: None)
        bar.show_preview(LocalFile(Path("/tmp/track.wav")), "Track")
        bar.player.playing_changed.emit(False)
        assert bar.state.text() == "PAUSED"
        bar.player.playing_changed.emit(True)
        assert bar.state.text() == "PLAYING LOCAL"

    def test_volume_slider_controls_the_local_audio_output(self, window):
        bar = window.download_bar
        bar.volume.setValue(35)
        assert bar.player._volume == pytest.approx(0.35)

    def test_downloading_while_streaming_says_both(self, window, monkeypatch):
        from neyta.providers.base import Embed

        monkeypatch.setattr(window.preview, "show_preview", lambda _preview: None)
        window.download_bar.show_preview(
            Embed("https://example.test/embed?autoplay=1"), "A Track"
        )
        window.download_bar.show_progress("A Track", 0.2, "downloading")
        assert window.download_bar.state.text() == "STREAMING + DOWNLOADING"

    def test_the_activity_strip_is_one_fixed_row(self, window):
        one_row = window.activity.preferred_height(rows=1)
        assert window.activity.height() == one_row
        assert window.activity.minimumHeight() == one_row
        assert window.activity.maximumHeight() == one_row

    def test_nothing_can_drag_it_taller(self, window):
        # No splitter to grab: it is a read-out, not a panel.
        assert not hasattr(window, "main_split")
        window.activity.resize(400, 300)
        assert window.activity.height() == window.activity.preferred_height()

    def test_status_messages_become_log_lines(self, window):
        window.statusBar().showMessage("Saved something.wav")
        assert any(line.message == "Saved something.wav"
                   for line in window.activity._lines)

    def test_a_finished_download_lands_in_the_tray(self, window, audio_files):
        from neyta.core.jobs import JobSnapshot, JobState

        snapshot = JobSnapshot(
            id=1, kind="download", label="x", state=JobState.SUCCEEDED,
            progress=1.0, message="", attempt=1, max_retries=0,
            result=audio_files[0],
        )
        window._on_job_succeeded(snapshot)
        assert window.tray.paths() == [audio_files[0]]


# ---------------------------------------------------------------------------
# The stem picker
# ---------------------------------------------------------------------------


class TestStemPicker:
    @pytest.fixture
    def picker(self, qapp, tmp_path):
        from neyta.core.stems import Calibration
        from neyta.ui.stempicker import StemPicker

        return StemPicker(Calibration(path=tmp_path / "cal.json"))

    def test_every_uvr_preset_is_offered(self, picker):
        # "All eight presets are exposed; nothing is hidden."
        offered = {config.stem_option(k).preset for k in picker._boxes}
        assert offered == {None, "vocals", "vocals_fast", "vocals_clean", "stems",
                           "stems6", "karaoke", "dereverb", "denoise"}

    def test_it_starts_on_the_default_selection(self, picker):
        assert picker.selection() == list(config.DEFAULT_STEMS)

    def test_ticking_changes_the_selection(self, picker):
        picker.set_selection(["vocals"])
        assert picker.selection() == ["vocals"]

    def test_selection_order_follows_the_picker_not_the_click_order(self, picker):
        picker.set_selection(["instrumental", "vocals"])
        assert picker.selection() == ["vocals", "instrumental"]

    def test_original_alone_runs_no_model(self, picker):
        picker.set_selection(["original"])
        assert not picker.runs_a_model()

    def test_vocals_runs_a_model(self, picker):
        picker.set_selection(["vocals"])
        assert picker.runs_a_model()

    def test_an_unmeasured_preset_does_not_invent_a_time(self, picker):
        picker.set_selection(["vocals"])
        picker.set_audio_seconds(240)
        assert "first run" in picker.estimate.text()

    def test_a_measured_preset_gives_a_real_estimate(self, picker):
        picker.calibration.record("vocals", 20, 23.2)  # 1.16x, measured here
        picker.set_selection(["vocals"])
        picker.set_audio_seconds(240)
        text = picker.estimate.text()
        assert "4m" in text and "this machine" in text

    def test_it_says_when_two_ticks_are_one_model_run(self, picker):
        picker.calibration.record("vocals", 20, 20)
        picker.set_selection(["vocals", "instrumental"])
        picker.set_audio_seconds(60)
        assert "one model run" in picker.estimate.text()

    def test_nothing_ticked_disables_separate(self, picker):
        picker.clear()
        assert not picker.separate_button.isEnabled()
        assert "nothing ticked" in picker.estimate.text()

    def test_a_preset_needing_a_download_is_marked(self, picker):
        from neyta.core import stems as stems_core

        if not stems_core.missing_models("karaoke"):
            pytest.skip("the karaoke checkpoint is already downloaded")
        assert "⤓" in picker._boxes["karaoke"].text()


class TestStemPickerOnACloudEngine:
    """The picker pointed at an engine that cannot do everything."""

    @pytest.fixture
    def picker(self, qapp, tmp_path):
        from neyta.core.lalal import LalalSeparator
        from neyta.core.stems import Calibration
        from neyta.ui.stempicker import StemPicker

        return StemPicker(
            Calibration(path=tmp_path / "cal.json"),
            separator=LalalSeparator(api_key="k"),
        )

    def test_what_it_cannot_do_is_shown_and_disabled(self, picker):
        # Off rather than absent: a shorter list would look like the whole
        # truth about what separation can produce.
        assert picker._boxes["stems"].isEnabled() is False
        assert picker._boxes["vocals"].isEnabled() is True

    def test_a_disabled_box_says_which_engine_refused_it(self, picker):
        assert "LALAL.AI" in picker._boxes["stems"].toolTip()

    def test_a_remembered_selection_cannot_tick_what_it_cannot_do(self, picker):
        # A disabled checkbox can still be checked in code, and that is how a
        # separation gets asked for something it cannot deliver.
        picker.set_selection(["stems", "vocals"])
        assert picker.selection() == ["vocals"]

    def test_the_estimate_talks_about_the_plan_not_this_machine(self, picker):
        picker.set_selection(["vocals"])
        picker.set_audio_seconds(240)
        text = picker.estimate.text()
        assert "uploaded to LALAL.AI" in text
        assert "4.0 min of your plan" in text
        assert "this machine" not in text


# ---------------------------------------------------------------------------
# The export dialog
# ---------------------------------------------------------------------------


def aac_media(**kw):
    return Media(
        result=make_result(**kw),
        streams=(Stream(id="140", ext="m4a", bitrate_kbps=129.0, codec="aac"),),
    )


def open_export(window, *, mode="download", media=None, source=None,
                audio_seconds=None, separator=None):
    """The dialog exactly as the window builds it, minus the modal exec."""
    from neyta.ui.exportdialog import (
        ExportDialog, SourceInfo, stem_format_options,
    )

    options = (
        stem_format_options() if mode == "separate"
        else window.provider.format_options(media)
    )
    return ExportDialog(
        settings=window.settings,
        provider_key=window.tab_key,
        format_options=options,
        source=source if source is not None else SourceInfo.from_media(media),
        calibration=window.calibration,
        audio_seconds=audio_seconds,
        # None means "assume the local engine works", which is what these
        # tests want: whether uvr-local happens to be built on the machine
        # running the suite is not what any of them is about.
        separator=separator,
        mode=mode,
    )


class TestExportDialog:
    def test_it_offers_the_formats_of_the_tab_it_was_opened_from(self, window):
        def offered(dialog):
            return {dialog.format_box.itemData(i)
                    for i in range(dialog.format_box.count())}

        window.tabs.setCurrentIndex(0)
        youtube = offered(open_export(window))
        window.tabs.setCurrentIndex(2)
        bandcamp = offered(open_export(window))
        assert "mp4_video" in youtube and "mp4_video" not in bandcamp
        assert "flac_source" in bandcamp and "flac_source" not in youtube

    def test_the_soulseek_tab_offers_passthrough_first(self, window):
        window.tabs.setCurrentIndex(3)
        assert open_export(window).format_box.itemData(0) == "original"

    def test_it_states_the_original_format(self, window):
        dialog = open_export(window, media=aac_media())
        assert "M4A" in dialog.source_label.text()
        assert "129k" in dialog.source_label.text()

    def test_a_lossless_original_says_so_rather_than_guessing_a_bitrate(
        self, window
    ):
        window.tabs.setCurrentIndex(2)
        media = Media(
            result=make_result(provider="bandcamp"),
            streams=(Stream(id="flac", ext="flac", bitrate_kbps=None,
                            codec="flac"),),
            lossless=True,
        )
        dialog = open_export(window, media=media)
        assert "FLAC" in dialog.source_label.text()
        assert "lossless" in dialog.source_label.text()

    def test_an_unprobed_result_admits_it_does_not_know(self, window):
        assert "not known yet" in open_export(window).source_label.text()

    def test_the_format_matching_the_original_is_flagged(self, window):
        window.tabs.setCurrentIndex(0)
        dialog = open_export(window, media=aac_media())
        index = dialog.format_box.findData("m4a_source")
        assert "same as the original" in dialog.format_box.itemText(index)

    def test_an_upscale_is_announced(self, window):
        window.tabs.setCurrentIndex(0)
        dialog = open_export(window, media=aac_media())
        dialog.format_box.setCurrentIndex(dialog.format_box.findData("mp3_320"))
        assert "upscale" in dialog.format_note.text()

    def test_no_warning_for_an_honest_format(self, window):
        window.tabs.setCurrentIndex(0)
        dialog = open_export(window, media=aac_media())
        dialog.format_box.setCurrentIndex(dialog.format_box.findData("wav_48_24"))
        assert dialog.format_note.text() == ""

    def test_an_unavailable_format_is_disabled_rather_than_hidden(self, window):
        window.tabs.setCurrentIndex(2)  # Bandcamp
        media = Media(
            result=make_result(provider="bandcamp"),
            streams=(Stream(id="mp3-128", ext="mp3", bitrate_kbps=128,
                            codec="mp3"),),
        )
        dialog = open_export(window, media=media)
        index = dialog.format_box.findData("flac_source")
        assert index >= 0, "the option should still be listed"
        assert not dialog.format_box.model().item(index).isEnabled()

    def test_it_carries_the_stem_picker(self, window):
        window.settings.stem_selection = ["stems"]
        assert open_export(window).stem_picker.selection() == ["stems"]

    def test_the_estimate_uses_the_track_length_it_was_given(self, window):
        window.calibration.record("vocals", 20, 20)
        dialog = open_export(window, media=aac_media(duration=240.0),
                             audio_seconds=240.0)
        dialog.stem_picker.set_selection(["vocals"])
        assert "4m" in dialog.stem_picker.estimate.text()

    def test_confirming_remembers_the_format_for_that_tab(self, window):
        window.tabs.setCurrentIndex(0)
        dialog = open_export(window, media=aac_media())
        dialog.format_box.setCurrentIndex(dialog.format_box.findData("mp3_128"))
        dialog.accept()
        assert open_export(window).format_box.currentData() == "mp3_128"

    def test_confirming_remembers_the_separation(self, window):
        dialog = open_export(window)
        dialog.stem_picker.set_selection(["vocals", "instrumental"])
        dialog.accept()
        assert window.settings.stem_selection == ["vocals", "instrumental"]

    def test_cancelling_changes_nothing(self, window):
        window.settings.stem_selection = ["original"]
        dialog = open_export(window)
        dialog.stem_picker.set_selection(["stems6"])
        dialog.reject()
        assert window.settings.stem_selection == ["original"]

    def test_the_choice_is_what_the_queue_is_handed(self, window, tmp_path):
        dialog = open_export(window, media=aac_media())
        dialog.format_box.setCurrentIndex(dialog.format_box.findData("wav_44_16"))
        dialog.stem_picker.set_selection(["vocals"])
        choice = dialog.choice()
        assert choice.fmt.key == "wav_44_16"
        assert choice.stems == ["vocals"]
        assert choice.separates

    def test_no_separation_ticked_means_no_separation(self, window):
        dialog = open_export(window)
        dialog.stem_picker.set_selection(["original"])
        assert not dialog.choice().separates

    def test_separate_mode_offers_only_what_a_stem_can_be_written_as(
        self, window
    ):
        dialog = open_export(window, mode="separate")
        keys = {dialog.format_box.itemData(i)
                for i in range(dialog.format_box.count())}
        assert keys == {f.key for f in config.STEM_FORMATS}
        assert "mp4_video" not in keys and "original" not in keys

    def test_separate_with_nothing_ticked_cannot_be_confirmed(self, window):
        dialog = open_export(window, mode="separate")
        dialog.stem_picker.set_selection(["original"])
        assert not dialog.ok_button.isEnabled()
        dialog.stem_picker.set_selection(["vocals"])
        assert dialog.ok_button.isEnabled()

    def test_separate_reads_the_type_off_the_file_on_disk(
        self, window, audio_files
    ):
        from neyta.ui.exportdialog import SourceInfo

        dialog = open_export(window, mode="separate",
                             source=SourceInfo.from_file(audio_files[0]))
        assert "WAV" in dialog.source_label.text()


class TestExportInWindow:
    def test_downloading_asks_first(self, window, monkeypatch):
        asked = []
        monkeypatch.setattr(
            window, "ask_export", lambda **kw: asked.append(kw) or None
        )
        window.model.set_results([make_result()])
        window.model.apply_media(0, aac_media())
        window.table.selectRow(0)
        window.download_selected()
        assert asked and asked[0]["mode"] == "download"

    def test_cancelling_the_dialog_queues_nothing(self, window, monkeypatch):
        monkeypatch.setattr(window, "ask_export", lambda **kw: None)
        window.model.set_results([make_result()])
        window.model.apply_media(0, aac_media())
        window.table.selectRow(0)
        window.download_selected()
        assert not [j for j in window.services.queue.jobs()
                    if j.kind == "download"]

    def test_download_is_the_only_button_and_covers_separating(self, window):
        # The dialog it opens carries the stem picker, so a second button
        # would be the same question with the download left out.
        assert not hasattr(window, "separate_button")

    def test_a_file_already_in_the_tray_can_still_be_separated(
        self, window, audio_files, monkeypatch
    ):
        asked = []
        monkeypatch.setattr(
            window, "ask_export", lambda **kw: asked.append(kw) or None
        )
        window.tray.add(audio_files[0])
        window.tray.list.selectAll()
        window.tray.separate_requested.emit()
        assert asked and asked[0]["mode"] == "separate"

    def test_separating_with_an_empty_tray_says_so_and_asks_nothing(
        self, window, monkeypatch
    ):
        monkeypatch.setattr(
            window, "ask_export",
            lambda **kw: pytest.fail("nothing to separate"),
        )
        window.separate_selected()
        assert "Download something first" in window.statusBar().currentMessage()

    def test_a_choice_with_no_separation_queues_no_stem_job(
        self, window, audio_files
    ):
        from neyta.ui.exportdialog import ExportChoice

        window._queue_separation(
            audio_files[0], make_result(),
            ExportChoice(fmt=config.WAV_48_24, stems=["original"],
                         dest_dir=Path(window.settings.download_dir)),
        )
        assert not [j for j in window.services.queue.jobs() if j.kind == "stems"]

    def test_finished_stems_land_in_the_tray_with_their_names(
        self, window, audio_files
    ):
        from neyta.core.jobs import JobSnapshot, JobState

        window._on_job_succeeded(JobSnapshot(
            id=9, kind="stems", label="x", state=JobState.SUCCEEDED,
            progress=1.0, message="", attempt=1, max_retries=0,
            result={"vocals": audio_files[0], "drums": audio_files[1]},
        ))
        assert len(window.tray.paths()) == 2


# ---------------------------------------------------------------------------
# The phrase panel
# ---------------------------------------------------------------------------


def make_hit(**kw):
    from neyta.core.phrase import Hit

    base = dict(
        video_id="v", title="A video", url="https://youtu.be/v", uploader="Up",
        start_ms=12000, end_ms=14000, accuracy="word",
        context="some words around the match", matched="the match",
    )
    base.update(kw)
    return Hit(**base)


class TestWaveform:
    @pytest.fixture
    def wave(self, qapp):
        from neyta.ui.phrasepanel import WaveformView

        view = WaveformView()
        view.resize(400, 100)
        view._peaks = [0.5] * 200
        view.duration = 4.0
        view.start, view.end = 0.0, 4.0
        return view

    def test_the_span_starts_as_the_whole_clip(self, wave):
        assert wave.span == (0.0, 4.0)

    def test_handles_can_be_moved(self, wave):
        wave.set_span(1.0, 3.0)
        assert wave.span == (1.0, 3.0)

    def test_a_span_cannot_escape_the_clip(self, wave):
        wave.set_span(-5.0, 99.0)
        assert wave.span == (0.0, 4.0)

    def test_the_end_never_precedes_the_start(self, wave):
        wave.set_span(3.0, 1.0)
        assert wave.end >= wave.start

    def test_moving_a_handle_emits_the_new_span(self, wave):
        seen = []
        wave.span_changed.connect(lambda a, b: seen.append((a, b)))
        wave.set_span(0.5, 2.5)
        assert seen == [(0.5, 2.5)]

    def test_clearing_empties_it(self, wave):
        wave.clear()
        assert wave.duration == 0.0 and wave.span == (0.0, 0.0)

    def test_setting_a_span_with_no_clip_is_a_no_op(self, qapp):
        from neyta.ui.phrasepanel import WaveformView

        view = WaveformView()
        view.set_span(1.0, 2.0)
        assert view.span == (0.0, 0.0)

    def test_it_paints_without_a_clip(self, wave):
        # The empty state is what the panel shows most of the time.
        wave.clear()
        wave.grab()

    def test_it_paints_with_a_clip(self, wave):
        wave.grab()


class TestPhrasePanel:
    @pytest.fixture
    def panel(self, qapp):
        from neyta.ui.phrasepanel import PhrasePanel

        return PhrasePanel()

    def test_it_starts_collapsed_but_toggleable(self, panel):
        # isVisibleTo, not isVisible: an unshown parent makes every child
        # report invisible, which would make both halves of this vacuous.
        assert not panel.body.isVisibleTo(panel)
        panel.enabled.setChecked(True)
        assert panel.body.isVisibleTo(panel)

    def test_the_controls_stay_visible_when_collapsed(self, panel):
        # The checkbox is how phrase mode is turned on; collapsing the results
        # must not take it away.
        assert panel.enabled.isVisibleTo(panel)

    def test_the_empty_state_explains_itself(self, panel):
        assert "captions" in panel.summary.text()

    def test_hits_are_listed_with_their_badges(self, panel):
        from neyta.core.phrase import PhraseSearch

        search = PhraseSearch(phrase="x", searched=30, hits=[
            make_hit(accuracy="word"),
            make_hit(accuracy="line", start_ms=30000, end_ms=32000),
        ])
        panel.set_search(search)
        assert panel.hits.count() == 2
        assert "word-accurate" in panel.hits.item(0).text()
        assert "line-accurate" in panel.hits.item(1).text()

    def test_the_summary_states_the_reach(self, panel):
        from neyta.core.phrase import PhraseSearch

        panel.set_search(PhraseSearch(phrase="x", searched=30))
        assert "top 30 results" in panel.summary.text()

    def test_the_two_accuracies_are_coloured_differently(self, panel):
        from neyta.core.phrase import PhraseSearch
        from neyta.ui.phrasepanel import LINE_COLOUR, WORD_COLOUR

        panel.set_search(PhraseSearch(phrase="x", searched=2, hits=[
            make_hit(accuracy="word"),
            make_hit(accuracy="line"),
        ]))
        assert panel.hits.item(0).foreground().color() == WORD_COLOUR
        assert panel.hits.item(1).foreground().color() == LINE_COLOUR

    def test_selecting_a_hit_announces_it(self, panel):
        from neyta.core.phrase import PhraseSearch

        seen = []
        panel.hit_selected.connect(seen.append)
        panel.set_search(PhraseSearch(phrase="x", searched=1,
                                      hits=[make_hit()]))
        assert seen and seen[0].matched == "the match"

    def test_a_fuzzy_hit_shows_its_score(self, panel):
        from neyta.core.phrase import PhraseSearch

        panel.set_search(PhraseSearch(phrase="x", searched=1,
                                      hits=[make_hit(score=0.91)]))
        assert "0.91" in panel.hits.item(0).text()

    def test_an_exact_hit_shows_no_score(self, panel):
        from neyta.core.phrase import PhraseSearch

        panel.set_search(PhraseSearch(phrase="x", searched=1,
                                      hits=[make_hit(score=1.0)]))
        assert "~" not in panel.hits.item(0).text()

    def test_cut_is_disabled_until_a_hit_is_chosen(self, panel):
        assert not panel.grab_button.isEnabled()

    def test_cutting_falls_back_to_the_padded_span_with_no_clip(self, panel):
        from neyta.core.phrase import PhraseSearch

        requests = []
        panel.grab_requested.connect(
            lambda hit, a, b: requests.append((hit, a, b))
        )
        hit = make_hit()
        panel.set_search(PhraseSearch(phrase="x", searched=1, hits=[hit]))
        panel._grab()
        assert requests and requests[0][1:] == hit.padded()

    def test_clearing_the_search_resets_everything(self, panel):
        from neyta.core.phrase import PhraseSearch

        panel.set_search(PhraseSearch(phrase="x", searched=1, hits=[make_hit()]))
        panel.set_search(None)
        assert panel.hits.count() == 0
        assert panel.current_hit is None
        assert not panel.grab_button.isEnabled()


class TestPhraseInWindow:
    def test_the_panel_is_youtube_only(self, window):
        window.tabs.setCurrentIndex(0)
        assert window.phrase_panel.isVisibleTo(window)
        window.tabs.setCurrentIndex(1)
        assert not window.phrase_panel.isVisibleTo(window)

    def test_enabling_it_replaces_the_result_list(self, window):
        window.tabs.setCurrentIndex(0)
        window.phrase_panel.enabled.setChecked(True)
        assert not window.table.isVisibleTo(window)
        assert window.phrase_panel.body.isVisibleTo(window)

    def test_disabling_it_brings_the_result_list_back(self, window):
        window.tabs.setCurrentIndex(0)
        window.phrase_panel.enabled.setChecked(True)
        window.phrase_panel.enabled.setChecked(False)
        assert window.table.isVisibleTo(window)

    def test_the_search_box_says_what_it_now_searches(self, window):
        window.phrase_panel.enabled.setChecked(True)
        assert "captions" in window.search.placeholderText()

    def test_the_box_names_the_engine_the_words_will_go_to(self, window):
        window.tabs.setCurrentIndex(0)
        window.phrase_panel.enabled.setChecked(True)
        assert "YouTube" in window.search.placeholderText()

        window.settings.phrase_engine = "filmot"
        window.settings.set_credential("filmot", "api_key", "k")
        window._refresh_phrase_engine()
        assert "Filmot" in window.search.placeholderText()

    def test_the_index_engine_turns_off_the_near_misses_box(self, window):
        # Fuzzing belongs to the local matcher; an index lookup has no
        # transcript on this side to be approximately right about.
        assert window.phrase_panel.fuzzy.isEnabled()
        window.settings.phrase_engine = "filmot"
        window.settings.set_credential("filmot", "api_key", "k")
        window._refresh_phrase_engine()
        assert not window.phrase_panel.fuzzy.isEnabled()

    def test_the_empty_panel_describes_the_engine_in_force(self, window):
        assert "reads the" in window.phrase_panel.summary.text()
        window.settings.phrase_engine = "filmot"
        window.settings.set_credential("filmot", "api_key", "k")
        window._refresh_phrase_engine()
        assert "Filmot's caption index" in window.phrase_panel.summary.text()

    def test_a_paid_engine_choice_is_displayed_without_reading_its_key(self, window):
        window.phrase_panel.enabled.setChecked(True)
        window.settings.phrase_engine = "filmot"
        window._refresh_phrase_engine()
        assert "Filmot" in window.search.placeholderText()
        assert not window.phrase_panel.fuzzy.isEnabled()

    def test_the_choice_is_persisted(self, window):
        window.phrase_panel.enabled.setChecked(True)
        assert window.settings.get("phrase/enabled") is True

    def test_phrase_results_land_in_the_panel(self, window):
        from neyta.core.jobs import JobSnapshot, JobState
        from neyta.core.phrase import PhraseSearch

        search = PhraseSearch(phrase="x", searched=30, hits=[make_hit()])
        window._on_job_succeeded(JobSnapshot(
            id=3, kind="phrase", label="x", state=JobState.SUCCEEDED,
            progress=1.0, message="", attempt=1, max_retries=0, result=search,
        ))
        assert window.phrase_panel.hits.count() == 1

    def test_shuffling_turns_phrase_mode_off_so_the_results_show(self, window):
        window.tabs.setCurrentIndex(0)
        window.phrase_panel.enabled.setChecked(True)
        window._on_shuffled([FakeShuffleTrack()])
        assert not window.phrase_panel.enabled.isChecked()
        assert window.table.isVisibleTo(window)


# ---------------------------------------------------------------------------
# The settings dialog
# ---------------------------------------------------------------------------


class TestSettingsPage:
    @pytest.fixture
    def page(self, qapp, tmp_path):
        from neyta.settings import FakeKeyring, MemoryPrefs, SecretStore, Settings
        from neyta.ui.settingspage import SettingsPage
        from neyta.vendor.slskd_bootstrap import SlskdBootstrap

        paths = config.Paths.under(tmp_path).ensure()
        settings = Settings(paths=paths, prefs=MemoryPrefs(),
                            secrets=SecretStore(backend=FakeKeyring()))
        return SettingsPage(settings, bootstrap=SlskdBootstrap(paths))

    def test_it_renders_a_section_per_service(self, page):
        from neyta import settings as settings_mod

        assert set(page.sections) == {s.key for s in settings_mod.SERVICES}

    def test_the_soulseek_login_is_present(self, page):
        # "the prompt to login to be found in settings"
        fields = page.sections["soulseek"].fields
        assert {"username", "password", "share_dir"} <= set(fields)

    def test_the_password_field_is_masked(self, page):
        from PySide6.QtWidgets import QLineEdit

        password = page.sections["soulseek"].fields["password"]
        assert password.echoMode() == QLineEdit.Password

    def test_the_shared_folder_is_a_browse_field(self, page):
        from neyta.ui.settingspage import PathField

        assert isinstance(page.sections["soulseek"].fields["share_dir"], PathField)

    def test_building_settings_does_not_read_the_keychain(self, tmp_path):
        from neyta.settings import (
            FakeKeyring, MemoryPrefs, SecretStore, Settings,
        )
        from neyta.ui.settingspage import SettingsPage

        class CountingKeyring(FakeKeyring):
            def __init__(self):
                super().__init__()
                self.reads = []

            def get_password(self, service, username):
                self.reads.append((service, username))
                return super().get_password(service, username)

        backend = CountingKeyring()
        settings = Settings(
            paths=config.Paths.under(tmp_path).ensure(),
            prefs=MemoryPrefs(),
            secrets=SecretStore(backend=backend),
        )
        settings.secrets.set("soulseek", "password", "saved")

        settings_page = SettingsPage(settings)
        settings_page.reload()

        assert backend.reads == []

    def test_saving_puts_the_password_in_the_keychain(self, page):
        page.sections["soulseek"].fields["password"].setText("hunter2")
        page.save()
        assert page.settings.credential("soulseek", "password") == "hunter2"
        # ...and not into the plain preferences.
        assert not any(
            "hunter2" in (page.settings.prefs.get_raw(k) or "")
            for k in page.settings.prefs.keys()
        )

    def test_blank_secret_field_preserves_the_saved_value(self, page):
        page.settings.set_credential("soulseek", "password", "saved")
        page.reload()

        assert page.sections["soulseek"].fields["password"].text() == ""
        page.save()
        assert page.settings.credential("soulseek", "password") == "saved"

    def test_saving_stores_the_username_as_an_ordinary_preference(self, page):
        page.sections["soulseek"].fields["username"].setText("me")
        page.save()
        assert page.settings.credential("soulseek", "username") == "me"

    def test_an_incomplete_soulseek_login_does_not_block_saving(self, page):
        # The other three tabs work without it.
        page.sections["soulseek"].fields["username"].setText("me")
        page.save()
        assert not page.bootstrap.configured()

    def test_a_complete_login_writes_the_daemon_config(self, page, tmp_path):
        share = tmp_path / "Shared"
        share.mkdir()
        fields = page.sections["soulseek"].fields
        fields["username"].setText("me")
        fields["password"].setText("pw")
        fields["share_dir"].setText(str(share))
        page.save()
        assert page.bootstrap.configured()

    def test_clearing_a_service_empties_its_fields(self, page):
        page.sections["soulseek"].fields["password"].setText("pw")
        page.save()
        page.sections["soulseek"]._clear()
        assert page.sections["soulseek"].fields["password"].text() == ""
        assert page.settings.credential("soulseek", "password") is None

    def test_youtube_cookies_are_off_by_default(self, page):
        assert not page.use_cookies.isChecked()

    def test_the_cookie_toggle_is_persisted(self, page):
        page.use_cookies.setChecked(True)
        page.save()
        assert page.settings.get("youtube/use_cookies") is True

    def test_the_download_folder_is_editable(self, page, tmp_path):
        target = tmp_path / "Samples"
        target.mkdir()
        page.downloads.setText(str(target))
        page.save()
        assert Path(page.settings.download_dir) == target

    def test_the_daemon_section_warns_about_the_single_login(self, page):
        assert "disconnect" in page.soulseek_daemon.warning.text()

    def test_the_daemon_section_says_slskd_is_a_different_program(self, page):
        # People reasonably assume having the Soulseek app installed is
        # enough. It is not, and the page has to say so.
        from PySide6.QtWidgets import QLabel

        text = " ".join(
            label.text() for label in page.soulseek_daemon.findChildren(QLabel)
        )
        assert "different program" in text

    def test_start_is_disabled_until_configured(self, page):
        page.soulseek_daemon.refresh()
        assert not page.soulseek_daemon.start_button.isEnabled()

    def test_it_is_a_page_with_nothing_to_dismiss(self, page):
        from PySide6.QtWidgets import QDialog, QDialogButtonBox

        assert not isinstance(page, QDialog)
        assert page.findChildren(QDialogButtonBox) == []

    def test_reload_takes_the_values_from_where_they_live(self, page, tmp_path):
        target = tmp_path / "Beats"
        target.mkdir()
        page.downloads.setText("/somewhere/stale")
        page.settings.download_dir = target
        page.reload()
        assert page.downloads.text() == str(target)

    def test_saving_says_so(self, page):
        seen = []
        page.saved.connect(lambda: seen.append(True))
        page.save()
        assert seen == [True]


class TestEngineChoosers:
    """The two jobs that can be done here or bought."""

    @pytest.fixture
    def page(self, qapp, tmp_path):
        from neyta.settings import FakeKeyring, MemoryPrefs, SecretStore, Settings
        from neyta.ui.settingspage import SettingsPage

        paths = config.Paths.under(tmp_path).ensure()
        settings = Settings(paths=paths, prefs=MemoryPrefs(),
                            secrets=SecretStore(backend=FakeKeyring()))
        return SettingsPage(settings)

    def test_both_jobs_offer_a_choice(self, page):
        assert page.phrase_engine.box.count() == len(config.PHRASE_ENGINES)
        assert page.stem_engine.box.count() == len(config.STEM_ENGINES)

    def test_they_open_on_the_free_engine(self, page):
        assert page.phrase_engine.current() == "builtin"
        assert page.stem_engine.current() == "uvr"

    def test_choosing_a_paid_engine_is_persisted(self, page):
        page.stem_engine.set_current("lalal")
        page.save()
        assert page.settings.get("stems/engine") == "lalal"

    def test_a_paid_engine_explains_when_the_keychain_is_read(self, page):
        page.stem_engine.set_current("lalal")
        assert "only requested when this service runs" in page.stem_engine.note.text()

    def test_and_does_not_actually_become_the_engine_until_it_has_one(self, page):
        # Chosen is not the same as in force. Without a key it would fail on
        # the first separation, so the free one keeps running.
        page.stem_engine.set_current("lalal")
        page.save()
        assert page.settings.stem_engine.key == "uvr"

        page.sections["lalal"].fields["api_key"].setText("licence")
        page.save()
        assert page.settings.stem_engine.key == "lalal"

    def test_the_paid_engine_offers_an_explicit_plan_check(self, page):
        page.stem_engine.set_current("lalal")
        assert page.stem_engine.check_button.isVisibleTo(page.stem_engine)

    def test_the_free_engine_never_offers_one(self, page):
        page.settings.set_credential("lalal", "api_key", "licence")
        page.stem_engine.set_current("uvr")
        assert not page.stem_engine.check_button.isVisibleTo(page.stem_engine)

    def test_checking_a_plan_with_no_key_asks_for_one(self, page):
        page.stem_engine.set_current("lalal")
        page._check_stem_plan()
        assert "Paste a licence key" in page.stem_engine.note.text()

    def test_reload_shows_the_choice_not_the_fallback(self, page):
        # The box is where the choice is made; the fallback is what runs.
        page.settings.set("phrase/engine", "filmot")
        page.reload()
        assert page.phrase_engine.current() == "filmot"
        assert page.settings.phrase_engine.key == "builtin"
