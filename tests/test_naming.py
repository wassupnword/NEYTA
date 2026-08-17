"""Sanitisation, path-escape attempts, collision suffixes.

Track titles come from strangers on Soulseek and from YouTube uploaders, so
these are adversarial inputs, not hypotheticals.
"""

from __future__ import annotations

import pytest

from neyta.core import naming


class TestSanitise:
    def test_ordinary_title_survives(self):
        assert naming.sanitise("Aphex Twin — Xtal") == "Aphex Twin — Xtal"

    @pytest.mark.parametrize(
        "hostile",
        [
            "../../../etc/passwd",
            "..\\..\\windows\\system32",
            "/absolute/path",
            "foo/bar",
            "a\x00b",
            "....",
            "..",
            ".",
        ],
    )
    def test_no_path_structure_survives(self, hostile):
        out = naming.sanitise(hostile)
        assert "/" not in out
        assert "\\" not in out
        assert out not in ("", ".", "..")
        assert not out.startswith(".")

    def test_separators_become_underscores_rather_than_truncating(self):
        # Path(x).name would silently drop the artist here.
        assert naming.sanitise("Artist/../Title") == "Artist_.._Title"

    def test_control_characters_become_spaces_not_deletions(self):
        # "a\nb" is two words. Deleting the newline would fuse them.
        assert naming.sanitise("a\nb\tc\x7f") == "a b c"

    def test_colon_is_replaced(self):
        assert ":" not in naming.sanitise("Track: The Remix")

    def test_whitespace_is_collapsed_and_trimmed(self):
        assert naming.sanitise("  a   b  ") == "a b"

    def test_empty_input_gets_the_fallback(self):
        assert naming.sanitise("") == "untitled"
        assert naming.sanitise("   ") == "untitled"
        assert naming.sanitise("...") == "untitled"

    def test_punctuation_only_titles_are_preserved_not_replaced(self):
        # "///" becomes "___" rather than falling back. Tempting to treat a
        # name with no letters as empty, but "!!!" is a real artist and this
        # is the same rule.
        assert naming.sanitise("///", fallback="track") == "___"
        assert naming.sanitise("!!!") == "!!!"

    def test_reserved_device_names_are_neutralised(self):
        assert naming.sanitise("CON") != "CON"
        assert naming.sanitise("nul.mp3").startswith("_")

    def test_long_names_are_capped_without_trailing_space(self):
        out = naming.sanitise("x" * 500)
        assert len(out) <= naming.MAX_COMPONENT
        assert out == out.rstrip(" .")

    def test_unicode_is_preserved(self):
        assert naming.sanitise("坂本龍一 — 戦場のメリークリスマス").startswith("坂本龍一")


class TestOutputName:
    def test_full_form(self):
        assert naming.output_name(
            artist="Boards of Canada", title="Roygbiv", stem="vocals", ext="wav"
        ) == "Boards of Canada - Roygbiv [vocals].wav"

    def test_no_artist(self):
        assert naming.output_name(title="Roygbiv", ext="wav") == "Roygbiv.wav"

    def test_no_stem(self):
        assert naming.output_name(
            artist="BoC", title="Roygbiv", ext="mp3"
        ) == "BoC - Roygbiv.mp3"

    def test_blank_artist_is_dropped_not_rendered(self):
        assert naming.output_name(artist="   ", title="X", ext="wav") == "X.wav"

    def test_leading_dot_on_extension_is_tolerated(self):
        assert naming.output_name(title="X", ext=".wav") == "X.wav"

    def test_hostile_title_cannot_escape(self):
        name = naming.output_name(title="../../evil", ext="wav")
        assert "/" not in name

    def test_hostile_stem_cannot_escape(self):
        name = naming.output_name(title="X", stem="../y", ext="wav")
        assert "/" not in name

    def test_a_very_long_name_keeps_its_extension(self):
        name = naming.output_name(
            artist="A" * 200, title="T" * 200, stem="vocals", ext="wav"
        )
        assert name.endswith(".wav")
        assert len(name) <= naming.MAX_COMPONENT * 2

    def test_a_very_long_extensionless_name_is_still_a_name(self):
        # Soulseek passthrough has no extension of its own. Trimming used to
        # produce a leading-dot hidden file here.
        name = naming.output_name(artist="A" * 200, title="T" * 200, ext="")
        assert not name.startswith(".")
        assert len(name) <= naming.MAX_COMPONENT * 2

    def test_extension_is_never_the_part_that_gets_trimmed(self):
        name = naming.output_name(title="T" * 400, ext="flac")
        assert name.endswith(".flac")


class TestCollisions:
    def test_free_path_is_used_as_is(self, tmp_path):
        assert naming.unique_path(tmp_path, "a.wav") == tmp_path / "a.wav"

    def test_first_collision_gets_2(self, tmp_path):
        (tmp_path / "a.wav").touch()
        assert naming.unique_path(tmp_path, "a.wav") == tmp_path / "a-2.wav"

    def test_collisions_keep_counting(self, tmp_path):
        (tmp_path / "a.wav").touch()
        (tmp_path / "a-2.wav").touch()
        (tmp_path / "a-3.wav").touch()
        assert naming.unique_path(tmp_path, "a.wav") == tmp_path / "a-4.wav"

    def test_extension_is_preserved_before_the_suffix(self, tmp_path):
        (tmp_path / "song.flac").touch()
        assert naming.unique_path(tmp_path, "song.flac").name == "song-2.flac"

    def test_nothing_on_disk_is_ever_returned(self, tmp_path):
        for n in ("a.wav", "a-2.wav"):
            (tmp_path / n).touch()
        assert not naming.unique_path(tmp_path, "a.wav").exists()

    def test_extensionless_names_still_get_a_suffix(self, tmp_path):
        (tmp_path / "raw").touch()
        assert naming.unique_path(tmp_path, "raw").name == "raw-2"


class TestResolveOutput:
    def test_lands_in_the_requested_directory(self, tmp_path):
        out = naming.resolve_output(tmp_path, title="Song", artist="A", ext="wav")
        assert out.parent == tmp_path.resolve()
        assert out.name == "A - Song.wav"

    @pytest.mark.parametrize(
        "title", ["../../escape", "/etc/passwd", "..", "a/b/c"]
    )
    def test_traversal_attempts_stay_inside(self, tmp_path, title):
        out = naming.resolve_output(tmp_path, title=title, ext="wav")
        assert out.parent == tmp_path.resolve()
        assert tmp_path.resolve() in out.parents

    def test_collision_is_resolved(self, tmp_path):
        first = naming.resolve_output(tmp_path, title="Song", ext="wav")
        first.touch()
        second = naming.resolve_output(tmp_path, title="Song", ext="wav")
        assert second.name == "Song-2.wav"
