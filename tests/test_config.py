"""Format matrix, upscale marking, stem presets, paths."""

from __future__ import annotations

import pytest

from neyta import config


class TestPaths:
    def test_under_puts_everything_in_one_root(self, tmp_path):
        p = config.Paths.under(tmp_path)
        for d in (p.support, p.cache, p.downloads, p.logs):
            assert tmp_path in d.parents or d.parent == tmp_path

    def test_ensure_creates_all_directories(self, tmp_path):
        p = config.Paths.under(tmp_path).ensure()
        for d in (p.support, p.cache, p.downloads, p.logs,
                  p.preview_dir, p.clips_dir):
            assert d.is_dir()

    def test_derived_paths_sit_under_their_parents(self, tmp_path):
        p = config.Paths.under(tmp_path)
        assert p.cache_db.parent == p.cache
        assert p.slskd_dir.parent == p.support

    def test_default_is_platform_appropriate(self):
        p = config.Paths.default()
        assert p.support.is_absolute()
        assert p.cache != p.support


class TestFormatMatrix:
    def test_every_provider_has_formats_and_a_default(self):
        for key in config.SOURCE_CEILING_KBPS:
            formats = config.formats_for(key)
            assert formats
            default = config.format_by_key(config.DEFAULT_FORMAT[key])
            assert default in formats

    def test_unknown_provider_raises(self):
        # Spotify used to be the example here, back when it was a service
        # NEYTA had no way to reach.
        with pytest.raises(ValueError):
            config.formats_for("napster")

    def test_unknown_format_raises(self):
        with pytest.raises(ValueError):
            config.format_by_key("flac_1411")

    def test_format_keys_are_unique(self):
        seen = {}
        for group in config.FORMATS.values():
            for fmt in group:
                assert seen.setdefault(fmt.key, fmt) is fmt, (
                    f"two different formats share the key {fmt.key!r}"
                )

    def test_only_youtube_offers_video(self):
        for provider, formats in config.FORMATS.items():
            has_video = any(f.kind == "video" for f in formats)
            assert has_video == (provider == "youtube")

    def test_soulseek_defaults_to_passthrough(self):
        # The one tab whose source can beat everything else: do not touch it
        # by default.
        assert config.DEFAULT_FORMAT["soulseek"] == config.ORIGINAL.key


class TestUpscaleMarking:
    """Build plan 2.1 — nothing may ship a misleading 320 by accident."""

    def test_mp3_320_from_youtube_is_an_upscale(self):
        source = config.SOURCE_CEILING_KBPS["youtube"]
        assert config.is_upscale(config.MP3_320, source)
        note = config.upscale_note(config.MP3_320, source)
        assert note and "129k" in note and "upscale" in note

    def test_mp3_320_from_soundcloud_is_an_upscale(self):
        assert config.is_upscale(config.MP3_320, config.SOURCE_CEILING_KBPS["soundcloud"])

    def test_mp3_128_from_youtube_is_not_an_upscale(self):
        source = config.SOURCE_CEILING_KBPS["youtube"]
        assert not config.is_upscale(config.MP3_128, source)
        assert config.upscale_note(config.MP3_128, source) is None

    @pytest.mark.parametrize("fmt", [config.WAV_48_24, config.WAV_44_16])
    def test_wav_is_never_an_upscale(self, fmt):
        # A decode of a 128k source is the honest full decode of that source.
        # UVR needs PCM, Ableton wants it, and it is never discouraged.
        for source in (128, 129, 160, 320, 1411, None):
            assert not config.is_upscale(fmt, source)

    def test_passthrough_is_never_an_upscale(self):
        assert not config.is_upscale(config.ORIGINAL, 128)
        assert not config.is_upscale(config.M4A_SOURCE, 128)

    def test_unknown_source_bitrate_marks_nothing(self):
        # Soulseek before a probe: claiming an upscale we cannot prove would
        # be as dishonest as hiding one.
        assert not config.is_upscale(config.MP3_320, None)

    def test_320_from_a_true_320_source_is_clean(self):
        assert not config.is_upscale(config.MP3_320, 320)

    def test_annotate_covers_every_format_for_the_tab(self):
        annotated = config.annotate_formats("youtube", 129)
        assert len(annotated) == len(config.formats_for("youtube"))
        marked = {f.key for f, note in annotated if note}
        assert marked == {"mp3_320", "mp3_192"}

    def test_soulseek_flac_source_marks_no_mp3_option(self):
        annotated = config.annotate_formats("soulseek", 1411)
        assert all(note is None for _, note in annotated)


class TestStems:
    def test_all_uvr_presets_are_exposed(self):
        # "All eight presets in uvr-local/uvr.py are exposed; nothing is
        # hidden." If uvr.py grows a preset, this fails until it is surfaced.
        exposed = {o.preset for o in config.STEM_OPTIONS if o.preset}
        assert exposed == {
            "vocals", "vocals_fast", "vocals_clean",
            "stems", "stems6", "karaoke", "dereverb", "denoise",
        }

    def test_original_runs_no_model(self):
        assert config.stem_option("original").preset is None

    def test_vocals_and_instrumental_share_one_run(self):
        # Both come out of the same BS-Roformer pass; ticking both must not
        # run the model twice.
        assert config.presets_for(["vocals", "instrumental"]) == ["vocals"]

    def test_original_contributes_no_preset(self):
        assert config.presets_for(["original"]) == []

    def test_presets_are_deduplicated_and_ordered(self):
        assert config.presets_for(
            ["stems", "vocals", "stems", "original", "karaoke"]
        ) == ["stems", "vocals", "karaoke"]

    def test_unknown_stem_option_raises(self):
        with pytest.raises(ValueError):
            config.stem_option("kick_only")

    def test_stem_keys_are_unique(self):
        keys = [o.key for o in config.STEM_OPTIONS]
        assert len(keys) == len(set(keys))


class TestCeilings:
    def test_every_provider_has_a_stated_ceiling_note(self):
        for key in config.SOURCE_CEILING_KBPS:
            assert config.CEILING_NOTE[key]

    def test_soulseek_ceiling_is_unknown_not_zero(self):
        # Peer-dependent. None means "ask the peer", not "no audio".
        assert config.SOURCE_CEILING_KBPS["soulseek"] is None
