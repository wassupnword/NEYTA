"""ffmpeg layer: argument construction, banner parsing, and real conversions.

The pure functions are tested with no subprocess at all. The tests that
actually run ffmpeg use the checked-in tone.wav fixture and skip cleanly where
there is no ffmpeg.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from neyta import config
from neyta.core import convert

FIXTURES = Path(__file__).parent / "fixtures"
TONE = FIXTURES / "tone.wav"

needs_ffmpeg = pytest.mark.skipif(
    config.find_ffmpeg() is None, reason="no ffmpeg — run tools/setup.sh"
)


# ---------------------------------------------------------------------------
# Pure argument construction
# ---------------------------------------------------------------------------


class TestCodecArgs:
    def test_wav_48_24_asks_for_24_bit_at_48k(self):
        args = convert.codec_args(config.WAV_48_24)
        assert "pcm_s24le" in args
        assert args[args.index("-ar") + 1] == "48000"

    def test_wav_44_16_asks_for_16_bit_at_44_1k(self):
        args = convert.codec_args(config.WAV_44_16)
        assert "pcm_s16le" in args
        assert args[args.index("-ar") + 1] == "44100"

    def test_mp3_carries_its_target_bitrate(self):
        assert "320k" in convert.codec_args(config.MP3_320)
        assert "128k" in convert.codec_args(config.MP3_128)

    def test_mp3_uses_lame(self):
        assert "libmp3lame" in convert.codec_args(config.MP3_320)

    def test_source_formats_stream_copy(self):
        assert "copy" in convert.codec_args(config.M4A_SOURCE)
        assert "copy" in convert.codec_args(config.ORIGINAL)

    def test_audio_formats_drop_the_video_stream(self):
        for fmt in (config.WAV_48_24, config.MP3_320, config.M4A_SOURCE):
            assert "-vn" in convert.codec_args(fmt)

    def test_video_format_keeps_video(self):
        args = convert.codec_args(config.MP4_VIDEO)
        assert "-vn" not in args

    def test_channel_count_is_never_forced(self):
        # Upmixing a mono source to stereo doubles the file and adds nothing,
        # which is the same argument that makes MP3 320 from 128k an upscale.
        for fmt in (config.WAV_48_24, config.WAV_44_16, config.MP3_320):
            assert "-ac" not in convert.codec_args(fmt)

    def test_an_unmappable_pcm_format_raises(self):
        weird = config.OutputFormat(
            "wav_96_32", "WAV 96/32", "wav", "pcm", sample_rate=96000, bit_depth=32
        )
        with pytest.raises(convert.ConversionError):
            convert.codec_args(weird)

    def test_an_unmappable_lossy_format_raises(self):
        weird = config.OutputFormat("ogg_q5", "OGG", "ogg", "lossy", bitrate_kbps=160)
        with pytest.raises(convert.ConversionError):
            convert.codec_args(weird)


class TestTrimArgs:
    def test_no_span_produces_no_arguments(self):
        assert convert.trim_args(None, None) == []

    def test_start_only_seeks(self):
        assert convert.trim_args(12.5, None) == ["-ss", "12.500"]

    def test_a_span_becomes_a_seek_plus_a_duration(self):
        # -t rather than -to: the meaning of -to relative to a preceding -ss
        # has changed between ffmpeg releases; a duration is unambiguous.
        assert convert.trim_args(10.0, 18.0) == ["-ss", "10.000", "-t", "8.000"]

    def test_a_span_starting_at_zero_still_gets_a_duration(self):
        assert convert.trim_args(0.0, 5.0) == ["-t", "5.000"]

    def test_an_inverted_span_raises(self):
        with pytest.raises(convert.ConversionError):
            convert.trim_args(20.0, 10.0)

    def test_a_zero_length_span_raises(self):
        with pytest.raises(convert.ConversionError):
            convert.trim_args(10.0, 10.0)

    def test_millisecond_precision_survives(self):
        assert convert.trim_args(1.2345, None) == ["-ss", "1.234"]


@needs_ffmpeg
class TestBuildCommand:
    def test_input_comes_after_the_seek(self, tmp_path):
        cmd = convert.build_command(TONE, tmp_path / "o.wav", config.WAV_48_24,
                                    start=5.0, end=7.0)
        assert cmd.index("-ss") < cmd.index("-i")

    def test_output_is_last(self, tmp_path):
        dest = tmp_path / "o.wav"
        assert convert.build_command(TONE, dest, config.WAV_48_24)[-1] == str(dest)

    def test_it_never_prompts(self, tmp_path):
        cmd = convert.build_command(TONE, tmp_path / "o.wav", config.WAV_48_24)
        assert "-y" in cmd and "-nostdin" in cmd


# ---------------------------------------------------------------------------
# Banner parsing
# ---------------------------------------------------------------------------

_BANNER_WAV = """
Input #0, wav, from '/tmp/x.wav':
  Duration: 00:00:01.80, bitrate: 705 kb/s
  Stream #0:0: Audio: pcm_s16le ([1][0][0][0] / 0x0001), 44100 Hz, mono, s16, 705 kb/s
"""

_BANNER_M4A = """
Input #0, mov,mp4,m4a,3gp,3g2,mj2, from '/tmp/x.m4a':
  Duration: 00:04:02.49, start: 0.000000, bitrate: 161 kb/s
  Stream #0:0[0x1](und): Audio: aac (LC) (mp4a / 0x6134706D), 44100 Hz, stereo, fltp, 160 kb/s
"""

_BANNER_LONG = """
  Duration: 02:03:04.50, bitrate: 129 kb/s
  Stream #0:0: Audio: opus, 48000 Hz, stereo, fltp, 128 kb/s
"""

_BANNER_EMPTY_MP4 = """
Input #0, mov,mp4,m4a,3gp,3g2,mj2, from '/tmp/source.m4a':
  Metadata:
    major_brand     : isom
  Duration: N/A, bitrate: N/A
"""


class TestParseBanner:
    def test_wav(self):
        info = convert.parse_banner(_BANNER_WAV)
        assert info.duration == pytest.approx(1.80)
        assert info.sample_rate == 44100
        assert info.channels == 1
        assert info.codec == "pcm_s16le"

    def test_m4a(self):
        info = convert.parse_banner(_BANNER_M4A)
        assert info.duration == pytest.approx(242.49)
        assert info.sample_rate == 44100
        assert info.channels == 2
        assert info.codec == "aac"
        assert info.bitrate_kbps == 160  # the stream, not the container's 161

    def test_hours_are_handled(self):
        assert convert.parse_banner(_BANNER_LONG).duration == pytest.approx(7384.5)

    def test_stereo_is_detected(self):
        assert convert.parse_banner(_BANNER_M4A).is_stereo
        assert not convert.parse_banner(_BANNER_WAV).is_stereo

    def test_an_empty_container_yields_no_duration(self):
        # This is exactly what SoundCloud's HLS returns for a ranged request:
        # a well-formed MP4 with no media in it and exit code 0.
        info = convert.parse_banner(_BANNER_EMPTY_MP4)
        assert info.duration is None
        assert info.codec is None

    def test_nonsense_input_yields_an_empty_info_rather_than_raising(self):
        assert convert.parse_banner("total gibberish") == convert.AudioInfo()


class TestParseSilence:
    def test_a_single_span(self):
        log = "[silencedetect] silence_start: 1.5\n[silencedetect] silence_end: 2.75\n"
        assert convert.parse_silence(log) == [convert.Span(1.5, 2.75)]

    def test_several_spans(self):
        log = (
            "silence_start: 0\nsilence_end: 0.4\n"
            "silence_start: 1.4\nsilence_end: 1.8\n"
        )
        spans = convert.parse_silence(log)
        assert [(s.start, s.end) for s in spans] == [(0.0, 0.4), (1.4, 1.8)]

    def test_a_negative_start_is_clamped_to_zero(self):
        assert convert.parse_silence("silence_start: -0.03\nsilence_end: 0.4\n")[0].start == 0.0

    def test_a_trailing_silence_is_closed_at_the_duration(self):
        spans = convert.parse_silence("silence_start: 1.4\n", duration=1.8)
        assert spans == [convert.Span(1.4, 1.8)]

    def test_a_trailing_silence_with_no_duration_is_dropped(self):
        assert convert.parse_silence("silence_start: 1.4\n") == []

    def test_no_silence_is_an_empty_list(self):
        assert convert.parse_silence("nothing here") == []

    def test_span_duration(self):
        assert convert.Span(1.0, 3.5).duration == 2.5
        assert convert.Span(3.0, 1.0).duration == 0.0


# ---------------------------------------------------------------------------
# Real conversions
# ---------------------------------------------------------------------------


@needs_ffmpeg
class TestProbe:
    def test_the_fixture_reads_back_as_written(self):
        info = convert.probe(TONE)
        assert info.duration == pytest.approx(1.8, abs=0.01)
        assert info.sample_rate == 44100
        assert info.channels == 1
        assert info.codec == "pcm_s16le"

    def test_a_missing_file_raises(self, tmp_path):
        with pytest.raises(convert.ConversionError):
            convert.probe(tmp_path / "nope.wav")

    def test_a_file_that_is_not_audio_raises(self, tmp_path):
        junk = tmp_path / "junk.wav"
        junk.write_bytes(b"this is not a wav file" * 100)
        with pytest.raises(convert.ConversionError):
            convert.probe(junk)


@needs_ffmpeg
class TestTranscode:
    def test_wav_48_24_is_actually_48k_and_24_bit(self, tmp_path):
        out = convert.transcode(TONE, tmp_path / "o.wav", config.WAV_48_24)
        info = convert.probe(out)
        assert info.sample_rate == 48000
        assert info.codec == "pcm_s24le"
        assert info.duration == pytest.approx(1.8, abs=0.02)

    def test_wav_44_16_round_trips_unchanged(self, tmp_path):
        info = convert.probe(
            convert.transcode(TONE, tmp_path / "o.wav", config.WAV_44_16)
        )
        assert info.sample_rate == 44100
        assert info.codec == "pcm_s16le"

    def test_mono_stays_mono(self, tmp_path):
        # No silent upmixing.
        info = convert.probe(
            convert.transcode(TONE, tmp_path / "o.wav", config.WAV_48_24)
        )
        assert info.channels == 1

    def test_mp3_320_is_produced_at_320(self, tmp_path):
        info = convert.probe(convert.transcode(TONE, tmp_path / "o.mp3", config.MP3_320))
        assert info.codec == "mp3"
        assert info.bitrate_kbps == pytest.approx(320, abs=8)

    def test_a_span_produces_exactly_that_much_audio(self, tmp_path):
        out = convert.transcode(
            TONE, tmp_path / "o.wav", config.WAV_48_24, start=0.4, end=1.0
        )
        assert convert.probe(out).duration == pytest.approx(0.6, abs=0.02)

    def test_the_parent_directory_is_created(self, tmp_path):
        out = convert.transcode(
            TONE, tmp_path / "a" / "b" / "o.wav", config.WAV_48_24
        )
        assert out.exists()

    def test_progress_is_reported_and_ends_at_one(self, tmp_path):
        seen: list[float] = []
        convert.transcode(
            TONE, tmp_path / "o.wav", config.WAV_48_24,
            progress=lambda f, m: seen.append(f),
        )
        assert seen and seen[-1] == 1.0
        assert all(0.0 <= f <= 1.0 for f in seen)

    def test_a_missing_source_raises(self, tmp_path):
        with pytest.raises(convert.ConversionError):
            convert.transcode(tmp_path / "nope.wav", tmp_path / "o.wav",
                              config.WAV_48_24)

    def test_failure_includes_ffmpegs_own_message(self, tmp_path):
        junk = tmp_path / "junk.wav"
        junk.write_bytes(b"not audio" * 200)
        with pytest.raises(convert.ConversionError) as exc:
            convert.transcode(junk, tmp_path / "o.wav", config.WAV_48_24)
        assert "ffmpeg" in str(exc.value).lower()


@needs_ffmpeg
class TestSilenceDetection:
    def test_the_fixtures_lead_in_and_lead_out_are_found(self):
        spans = convert.detect_silence(TONE, min_silence=0.2)
        assert len(spans) == 2
        assert spans[0].start == pytest.approx(0.0, abs=0.05)
        assert spans[0].end == pytest.approx(0.4, abs=0.05)
        assert spans[1].end == pytest.approx(1.8, abs=0.05)

    def test_tighten_lands_on_the_tone(self):
        span = convert.tighten(TONE, min_silence=0.2)
        assert span is not None
        assert span.start == pytest.approx(0.4, abs=0.05)
        assert span.end == pytest.approx(1.4, abs=0.05)

    def test_tighten_on_pure_silence_returns_none(self, tmp_path):
        # Better to leave the padded cut alone than to write a zero-length file.
        silent = tmp_path / "silent.wav"
        convert.transcode(TONE, silent, config.WAV_44_16, start=0.0, end=0.35)
        assert convert.tighten(silent, min_silence=0.1) is None

    def test_a_tightened_span_can_be_cut(self, tmp_path):
        span = convert.tighten(TONE, min_silence=0.2)
        out = convert.transcode(
            TONE, tmp_path / "tight.wav", config.WAV_48_24,
            start=span.start, end=span.end,
        )
        assert convert.probe(out).duration == pytest.approx(1.0, abs=0.05)


class TestFfmpegDiscovery:
    def test_an_override_is_honoured(self, tmp_path):
        assert convert.ffmpeg_binary(tmp_path / "custom") == tmp_path / "custom"

    def test_a_missing_ffmpeg_raises_with_a_fix(self, monkeypatch):
        monkeypatch.setattr(convert.config, "find_ffmpeg", lambda: None)
        with pytest.raises(convert.FfmpegMissing) as exc:
            convert.ffmpeg_binary()
        assert "setup.sh" in str(exc.value)
