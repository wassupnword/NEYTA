"""Deleting a downloaded file.

The Downloaded page lists a real folder, so its delete is a real delete. It
goes to the Trash rather than to `unlink`, which is the whole point of these
tests: a mis-clicked delete has to be something you can walk back.
"""

from __future__ import annotations

import sys

import pytest

from neyta.core import trash


@pytest.fixture
def bin_dir(tmp_path):
    return tmp_path / "Trash"


@pytest.fixture
def song(tmp_path):
    path = tmp_path / "Artist - Song.wav"
    path.write_bytes(b"RIFF" + b"\0" * 64)
    return path


class TestTrashDir:
    def test_it_is_the_users_own_trash(self, tmp_path):
        found = trash.trash_dir(home=tmp_path)
        assert str(found).startswith(str(tmp_path))
        expected = ".Trash" if sys.platform == "darwin" else "Trash"
        assert expected in str(found)


class TestMoveToTrash:
    def test_the_file_leaves_the_folder(self, song, bin_dir):
        trash.move_to_trash(song, trash=bin_dir)
        assert not song.exists()

    def test_it_is_still_there_to_get_back(self, song, bin_dir):
        removal = trash.move_to_trash(song, trash=bin_dir)
        assert removal.destination == bin_dir / song.name
        assert removal.destination.exists()
        assert removal.recoverable

    def test_the_bytes_survive_the_trip(self, song, bin_dir):
        before = song.read_bytes()
        removal = trash.move_to_trash(song, trash=bin_dir)
        assert removal.destination.read_bytes() == before

    def test_the_trash_is_made_if_it_is_not_there(self, song, bin_dir):
        assert not bin_dir.exists()
        trash.move_to_trash(song, trash=bin_dir)
        assert bin_dir.is_dir()

    def test_a_second_file_of_the_same_name_does_not_overwrite_the_first(
        self, tmp_path, bin_dir
    ):
        first = tmp_path / "a" / "Song.wav"
        second = tmp_path / "b" / "Song.wav"
        for path, content in ((first, b"one"), (second, b"two")):
            path.parent.mkdir()
            path.write_bytes(content)

        trash.move_to_trash(first, trash=bin_dir)
        removal = trash.move_to_trash(second, trash=bin_dir)
        assert (bin_dir / "Song.wav").read_bytes() == b"one"
        assert removal.destination == bin_dir / "Song 2.wav"
        assert removal.destination.read_bytes() == b"two"

    def test_a_missing_file_says_so_rather_than_pretending(self, tmp_path,
                                                           bin_dir):
        with pytest.raises(FileNotFoundError):
            trash.move_to_trash(tmp_path / "gone.wav", trash=bin_dir)

    def test_an_unusable_trash_is_reported_not_swallowed(self, song, tmp_path):
        # A file where the Trash should be: mkdir fails, and the caller has
        # to hear about it rather than have the file quietly unlinked.
        blocked = tmp_path / "blocked"
        blocked.write_text("not a directory")
        with pytest.raises(trash.TrashUnavailable):
            trash.move_to_trash(song, trash=blocked)
        assert song.exists(), "nothing is deleted when the Trash is unusable"


class TestDelete:
    def test_it_removes_the_file(self, song):
        trash.delete(song)
        assert not song.exists()

    def test_it_says_the_file_is_not_coming_back(self, song):
        assert not trash.delete(song).recoverable
