"""The UVR driver, the calibration, and the naming of what comes out.

The driver is exercised against a stub runner that speaks the same JSON
protocol, so these run in seconds and never load torch. The real thing is
covered by the `env`-marked test at the bottom.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from neyta import config
from neyta.core import stems as S

FAKE_RUNNER = Path(__file__).parent / "fixtures" / "fake_uvr_runner.py"


@pytest.fixture
def separator(tmp_path, monkeypatch):
    """A driver wired to the stub runner and NEYTA's own interpreter.

    The fake uvr root only has to look built — the stub runner never imports
    it — but `available()` checks for uvr.py, and that check is the point.
    """
    fake_root = tmp_path / "uvr"
    fake_root.mkdir()
    (fake_root / "uvr.py").write_text("# stand-in\n")
    monkeypatch.setenv("FAKE_UVR_MODE", "ok")
    return S.StemSeparator(
        python=Path(sys.executable),
        uvr_root=fake_root,
        calibration=S.Calibration(path=tmp_path / "cal.json"),
        runner=FAKE_RUNNER,
    )


@pytest.fixture
def audio(tmp_path):
    path = tmp_path / "input.wav"
    path.write_bytes(b"RIFF" + b"\0" * 1024)
    return path


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


class TestPlanSeparation:
    def test_original_runs_nothing(self):
        assert S.plan_separation(["original"]) == []

    def test_an_empty_selection_runs_nothing(self):
        assert S.plan_separation([]) == []

    def test_vocals_and_instrumental_are_one_model_run(self):
        steps = S.plan_separation(["vocals", "instrumental"])
        assert len(steps) == 1
        assert steps[0].preset == "vocals"
        assert steps[0].wanted == frozenset({"vocals", "instrumental"})

    def test_ticking_only_vocals_does_not_hand_back_instrumental(self):
        # The plan promises you get exactly what you ticked, even when the
        # other stem comes free out of the same run.
        step = S.plan_separation(["vocals"])[0]
        produced = {"vocals": Path("v.wav"), "instrumental": Path("i.wav")}
        assert set(step.keep(produced)) == {"vocals"}

    def test_an_all_stems_option_keeps_everything(self):
        step = S.plan_separation(["stems"])[0]
        assert step.wanted is None
        produced = {n: Path(f"{n}.wav") for n in ("drums", "bass", "other", "vocals")}
        assert step.keep(produced) == produced

    def test_an_all_stems_option_subsumes_a_named_one(self):
        # "stems" and a hypothetical named subset of the same preset must not
        # produce a step that filters away what the broader tick asked for.
        steps = S.plan_separation(["stems", "original"])
        assert len(steps) == 1 and steps[0].wanted is None

    def test_distinct_presets_become_distinct_steps(self):
        presets = {s.preset for s in S.plan_separation(["vocals", "stems", "denoise"])}
        assert presets == {"vocals", "stems", "denoise"}

    def test_duplicate_keys_do_not_duplicate_work(self):
        assert len(S.plan_separation(["vocals", "vocals", "vocals"])) == 1

    def test_an_unknown_option_raises(self):
        with pytest.raises(ValueError):
            S.plan_separation(["kick_only"])


class TestMissingModels:
    def test_a_downloaded_preset_reports_nothing_missing(self):
        if not (config.UVR_ROOT / "models").exists():
            pytest.skip("no models directory")
        assert S.missing_models("vocals") == []

    def test_karaoke_needs_a_checkpoint_that_is_not_shipped(self):
        if not (config.UVR_ROOT / "models").exists():
            pytest.skip("no models directory")
        # Worth surfacing: the first karaoke run downloads before it processes.
        assert "6_HP-Karaoke-UVR.pth" in S.missing_models("karaoke")

    def test_an_unknown_preset_needs_nothing(self):
        assert S.missing_models("nonexistent") == []


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------


class TestCalibration:
    def test_an_unmeasured_preset_has_no_estimate(self):
        assert S.Calibration().estimate("vocals", 240) is None

    def test_the_first_measurement_is_taken_at_face_value(self):
        cal = S.Calibration()
        cal.record("vocals", audio_seconds=20, elapsed=23.2)
        assert cal.rate("vocals") == pytest.approx(1.16)

    def test_an_estimate_scales_with_track_length(self):
        cal = S.Calibration()
        cal.record("vocals", 20, 23.2)
        assert cal.estimate("vocals", 240) == pytest.approx(240 * 1.16)

    def test_later_measurements_are_smoothed_not_replaced(self):
        # One run on a loaded machine should not poison the estimate.
        cal = S.Calibration(smoothing=0.4)
        cal.record("vocals", 20, 20.0)  # 1.0x
        cal.record("vocals", 20, 40.0)  # 2.0x, an outlier
        assert 1.0 < cal.rate("vocals") < 2.0
        assert cal.rate("vocals") == pytest.approx(1.4)

    def test_sample_counts_are_kept(self):
        cal = S.Calibration()
        for _ in range(3):
            cal.record("vocals", 20, 20)
        assert cal.samples["vocals"] == 3

    def test_nonsense_measurements_are_ignored(self):
        cal = S.Calibration()
        cal.record("vocals", 0, 10)
        cal.record("vocals", 10, 0)
        assert cal.rate("vocals") is None

    def test_it_persists(self, tmp_path):
        path = tmp_path / "cal.json"
        S.Calibration(path=path).record("vocals", 20, 23.2)
        assert S.Calibration(path=path).rate("vocals") == pytest.approx(1.16)

    def test_a_corrupt_file_starts_fresh_rather_than_crashing(self, tmp_path):
        path = tmp_path / "cal.json"
        path.write_text("{not json")
        assert S.Calibration(path=path).rates == {}

    def test_saving_leaves_no_temp_file(self, tmp_path):
        path = tmp_path / "cal.json"
        S.Calibration(path=path).record("vocals", 20, 20)
        assert [p.name for p in tmp_path.iterdir()] == ["cal.json"]


class TestEstimateAll:
    def test_a_fully_measured_selection_is_complete(self):
        cal = S.Calibration()
        cal.record("vocals", 20, 20)
        seconds, complete = cal.estimate_all(S.plan_separation(["vocals"]), 100)
        assert complete and seconds == pytest.approx(100)

    def test_an_unmeasured_preset_makes_the_total_a_lower_bound(self):
        cal = S.Calibration()
        cal.record("vocals", 20, 20)
        steps = S.plan_separation(["vocals", "stems"])
        seconds, complete = cal.estimate_all(steps, 100)
        assert not complete
        assert seconds == pytest.approx(100)

    def test_two_presets_add_up(self):
        cal = S.Calibration()
        cal.record("vocals", 20, 20)   # 1.0x
        cal.record("denoise", 20, 4)   # 0.2x
        steps = S.plan_separation(["vocals", "denoise"])
        seconds, complete = cal.estimate_all(steps, 100)
        assert complete and seconds == pytest.approx(120)


class TestDescribe:
    def test_no_separation_says_instant(self):
        assert "instant" in S.Calibration().describe([], 240)

    def test_an_unmeasured_preset_says_so_rather_than_guessing(self):
        text = S.Calibration().describe(S.plan_separation(["vocals"]), 240)
        assert "first run" in text
        assert "m" not in text.split("first run")[0]  # no invented duration

    def test_a_measured_preset_gives_a_time_on_this_machine(self):
        cal = S.Calibration()
        cal.record("vocals", 20, 23.2)
        text = cal.describe(S.plan_separation(["vocals"]), 240)
        assert "4m" in text and "this machine" in text

    def test_a_partly_measured_selection_says_at_least(self):
        cal = S.Calibration()
        cal.record("vocals", 20, 20)
        text = cal.describe(S.plan_separation(["vocals", "stems"]), 240)
        assert text.startswith("at least")

    def test_an_unknown_track_length_says_so(self):
        assert "length" in S.Calibration().describe(
            S.plan_separation(["vocals"]), 0
        )

    @pytest.mark.parametrize(
        "seconds,expected",
        [(5, "5s"), (59, "59s"), (65, "1m 05s"), (3700, "1h 01m")],
    )
    def test_human_durations(self, seconds, expected):
        assert S._human(seconds) == expected


# ---------------------------------------------------------------------------
# The driver
# ---------------------------------------------------------------------------


class TestDriver:
    def test_a_successful_run_returns_its_stems(self, separator, audio, tmp_path):
        result = separator.run_preset(audio, "vocals", tmp_path / "out")
        assert set(result.stems) == {"vocals", "instrumental"}
        assert all(p.exists() for p in result.stems.values())

    def test_the_output_directory_is_created(self, separator, audio, tmp_path):
        separator.run_preset(audio, "vocals", tmp_path / "deep" / "out")
        assert (tmp_path / "deep" / "out").is_dir()

    def test_a_missing_input_raises_before_launching_anything(
        self, separator, tmp_path
    ):
        with pytest.raises(S.StemError, match="no such file"):
            separator.run_preset(tmp_path / "nope.wav", "vocals", tmp_path / "out")

    def test_an_unbuilt_uvr_raises_with_a_fix(self, tmp_path, audio):
        separator = S.StemSeparator(
            python=tmp_path / "absent-python", uvr_root=tmp_path / "absent"
        )
        with pytest.raises(S.UvrUnavailable, match="setup.sh"):
            separator.run_preset(audio, "vocals", tmp_path / "out")

    def test_a_runner_error_surfaces_its_message(
        self, separator, audio, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("FAKE_UVR_MODE", "error")
        with pytest.raises(S.StemError, match="model failed to load"):
            separator.run_preset(audio, "vocals", tmp_path / "out")

    def test_a_crash_is_reported_with_the_exit_code(
        self, separator, audio, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("FAKE_UVR_MODE", "crash")
        with pytest.raises(S.StemError, match="no result"):
            separator.run_preset(audio, "vocals", tmp_path / "out")

    def test_a_silent_runner_is_an_error_not_an_empty_success(
        self, separator, audio, tmp_path, monkeypatch
    ):
        # Exit code 0 with no "done" event must not read as "no stems wanted".
        monkeypatch.setenv("FAKE_UVR_MODE", "silent")
        with pytest.raises(S.StemError):
            separator.run_preset(audio, "vocals", tmp_path / "out")

    def test_non_json_chatter_does_not_break_parsing(
        self, separator, audio, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("FAKE_UVR_MODE", "garbage")
        with pytest.raises(S.StemError):
            separator.run_preset(audio, "vocals", tmp_path / "out")

    def test_progress_is_reported_and_ends_at_one(self, separator, audio, tmp_path):
        seen: list[float] = []
        separator.run_preset(
            audio, "vocals", tmp_path / "out",
            progress=lambda f, m="": seen.append(f),
        )
        assert seen and seen[-1] == 1.0
        assert all(0.0 <= f <= 1.0 for f in seen)

    def test_progress_never_claims_completion_early(
        self, separator, audio, tmp_path, monkeypatch
    ):
        # The estimate can be wrong; the bar holds at 99% rather than sitting
        # at 100% while the model is still running.
        separator.calibration.record("vocals", 10, 0.1)  # a wildly low estimate
        monkeypatch.setenv("FAKE_UVR_SLEEP", "1.2")
        seen: list[float] = []
        separator.run_preset(
            audio, "vocals", tmp_path / "out", audio_seconds=10,
            progress=lambda f, m="": seen.append(f), poll=0.1,
        )
        assert max(seen[:-1]) <= 0.99

    def test_a_run_records_calibration(self, separator, audio, tmp_path):
        separator.run_preset(audio, "vocals", tmp_path / "out", audio_seconds=10)
        assert separator.calibration.rate("vocals") is not None

    def test_a_run_without_a_known_length_records_nothing(
        self, separator, audio, tmp_path
    ):
        separator.run_preset(audio, "vocals", tmp_path / "out", audio_seconds=None)
        assert separator.calibration.rates == {}

    def test_cancellation_stops_the_subprocess(
        self, separator, audio, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("FAKE_UVR_MODE", "slow")
        with pytest.raises(S.StemError, match="cancelled"):
            separator.run_preset(
                audio, "vocals", tmp_path / "out",
                should_cancel=lambda: True, poll=0.1,
            )


class TestSeparateSelection:
    def test_a_selection_with_no_models_does_nothing(
        self, separator, audio, tmp_path
    ):
        assert separator.separate(audio, ["original"], tmp_path / "out") == {}

    def test_one_preset_yields_the_stems_it_was_asked_for(
        self, separator, audio, tmp_path
    ):
        got = separator.separate(audio, ["vocals"], tmp_path / "out")
        assert set(got) == {"vocals"}

    def test_both_halves_of_one_run_are_kept_when_both_are_ticked(
        self, separator, audio, tmp_path
    ):
        got = separator.separate(audio, ["vocals", "instrumental"], tmp_path / "out")
        assert set(got) == {"vocals", "instrumental"}

    def test_two_presets_that_emit_the_same_stem_name_do_not_collide(
        self, separator, audio, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("FAKE_UVR_STEMS", "vocals")
        got = separator.separate(audio, ["vocals", "vocals_fast"], tmp_path / "out")
        assert len(got) == 2, "one stem was silently dropped"

    def test_progress_spans_the_whole_selection(self, separator, audio, tmp_path):
        seen: list[float] = []
        separator.separate(
            audio, ["vocals", "denoise"], tmp_path / "out",
            progress=lambda f, m="": seen.append(f),
        )
        assert seen[-1] == pytest.approx(1.0)
        assert seen == sorted(seen)


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


class TestDeliver:
    @pytest.fixture
    def raw(self, tmp_path):
        out = {}
        for name in ("vocals", "instrumental"):
            path = tmp_path / "scratch" / f"clip_({name})_model.wav"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"RIFF" + b"\0" * 64)
            out[name] = path
        return out

    def test_stems_are_named_for_the_track_not_the_model(self, raw, tmp_path):
        delivered = S.deliver(raw, tmp_path / "out", title="Xtal", artist="Aphex Twin")
        assert delivered["vocals"].name == "Aphex Twin - Xtal [vocals].wav"

    def test_no_artist_still_names_cleanly(self, raw, tmp_path):
        delivered = S.deliver(raw, tmp_path / "out", title="Xtal")
        assert delivered["vocals"].name == "Xtal [vocals].wav"

    def test_the_source_files_are_moved_not_copied(self, raw, tmp_path):
        S.deliver(raw, tmp_path / "out", title="Xtal")
        assert not raw["vocals"].exists()

    def test_a_second_separation_does_not_overwrite_the_first(
        self, raw, tmp_path
    ):
        S.deliver(raw, tmp_path / "out", title="Xtal")
        again = {}
        for name in ("vocals", "instrumental"):
            path = tmp_path / "scratch2" / f"clip_({name}).wav"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"RIFF")
            again[name] = path
        delivered = S.deliver(again, tmp_path / "out", title="Xtal")
        assert delivered["vocals"].name == "Xtal [vocals]-2.wav"

    def test_a_hostile_title_cannot_escape_the_output_folder(self, raw, tmp_path):
        delivered = S.deliver(raw, tmp_path / "out", title="../../../etc/passwd")
        assert delivered["vocals"].parent == (tmp_path / "out").resolve()

    def test_a_missing_stem_is_skipped_rather_than_crashing(self, raw, tmp_path):
        raw["ghost"] = tmp_path / "scratch" / "gone.wav"
        delivered = S.deliver(raw, tmp_path / "out", title="Xtal")
        assert "ghost" not in delivered
        assert len(delivered) == 2

    def test_the_extension_follows_the_produced_file(self, tmp_path):
        source = tmp_path / "s" / "x_(vocals).flac"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"fLaC")
        delivered = S.deliver({"vocals": source}, tmp_path / "out", title="X")
        assert delivered["vocals"].suffix == ".flac"


class TestProtocolParsing:
    def test_valid_json_parses(self):
        assert S._parse('{"event": "done"}') == {"event": "done"}

    def test_blank_lines_are_ignored(self):
        assert S._parse("   \n") is None

    def test_garbage_is_ignored_rather_than_raising(self):
        assert S._parse("Loading model: 45%|####") is None


# ---------------------------------------------------------------------------
# The real thing
# ---------------------------------------------------------------------------


@pytest.mark.env
class TestRealUvr:
    def test_the_driver_finds_the_real_uvr(self):
        separator = S.StemSeparator()
        if not separator.available():
            pytest.skip("uvr-local not built")
        assert separator.python.exists()

    def test_every_exposed_preset_has_its_models_listed(self):
        # If uvr.py grows a preset, _MODEL_FILES has to learn about it or the
        # "downloads on first use" warning goes quietly wrong.
        exposed = {o.preset for o in config.STEM_OPTIONS if o.preset}
        assert exposed <= set(S._MODEL_FILES)

    def test_the_runner_is_importable_by_the_other_interpreter(self):
        # It must not import neyta — that package does not exist over there.
        source = (Path(S.__file__).parent / "uvr_runner.py").read_text()
        assert "from neyta" not in source
        assert "import neyta" not in source
