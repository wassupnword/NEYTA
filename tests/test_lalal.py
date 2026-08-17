"""The cloud stem engine, offline.

A fake session plays the part of the API: upload hands back an id, split is
queued, check reports progress and then success, and the stem URLs resolve to
bytes. Nothing here touches the network or spends anyone's minutes.

The behaviours worth pinning are the ones that cost money if they are wrong:
one upload per track, one split for two ticked boxes that share it, and a
refusal to accept work this engine cannot do.
"""

from __future__ import annotations

import json

import pytest

from neyta import config
from neyta.core import lalal


class FakeResponse:
    def __init__(self, payload=None, status_code=200, body: bytes = b"") -> None:
        self._payload = payload
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._payload

    def iter_content(self, chunk_size=1):
        yield self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeApi:
    """Enough of LALAL.AI to drive the separator.

    `checks_before_done` is how many polls report progress before the split
    reports success, so the poll loop is exercised rather than short-circuited.
    """

    def __init__(self, checks_before_done: int = 1) -> None:
        self.checks_before_done = checks_before_done
        self.uploads = 0
        self.splits: list[dict] = []
        self.cancels = 0
        self.downloads: list[str] = []
        self._checks = 0
        self._stem = "vocals"

    # -- the two verbs the separator uses
    def post(self, url, headers=None, timeout=None, data=None, **kw):
        path = url.split("lalal.ai")[-1]
        if path == "/api/upload/":
            self.uploads += 1
            return FakeResponse({"status": "success", "id": "file-1",
                                 "duration": 180.0})
        if path == "/api/split/":
            params = json.loads(data["params"])[0]
            self.splits.append(params)
            self._stem = params["stem"]
            self._checks = 0
            return FakeResponse({"status": "success", "task_id": "t1"})
        if path == "/api/check/":
            self._checks += 1
            if self._checks <= self.checks_before_done:
                return FakeResponse({"status": "success", "result": {
                    "file-1": {"status": "success",
                               "task": {"state": "progress", "progress": 40}},
                }})
            return FakeResponse({"status": "success", "result": {
                "file-1": {
                    "status": "success",
                    "task": {"state": "success", "progress": 100},
                    "split": {
                        "stem": self._stem,
                        "stem_track": f"https://cdn/{self._stem}.wav",
                        "back_track": "https://cdn/back.wav",
                    },
                },
            }})
        if path == "/api/cancel/":
            self.cancels += 1
            return FakeResponse({"status": "success"})
        raise AssertionError(f"unexpected POST {path}")

    def get(self, url, params=None, timeout=None, stream=False, **kw):
        if url.endswith("/billing/get-limits/"):
            return FakeResponse({
                "status": "success", "option": "Plus",
                "email": "me@example.com",
                "process_duration_limit": 6000.0,
                "process_duration_used": 600.0,
                "process_duration_left": 5400.0,
            })
        self.downloads.append(url)
        return FakeResponse(body=b"RIFFfake")


@pytest.fixture
def api():
    return FakeApi()


@pytest.fixture
def separator(api):
    return lalal.LalalSeparator(
        api_key="licence", session=api, sleep=lambda _s: None,
    )


@pytest.fixture
def track(tmp_path):
    path = tmp_path / "A Song.wav"
    path.write_bytes(b"RIFF" + b"\0" * 64)
    return path


class TestAvailability:
    def test_a_key_is_the_whole_installation(self, separator):
        assert separator.available()

    def test_without_a_key_it_is_unavailable_and_says_what_to_do(self):
        engine = lalal.LalalSeparator(api_key="")
        assert not engine.available()
        assert "Settings" in engine.unavailable_note

    def test_it_only_offers_what_it_can_actually_produce(self, separator):
        assert separator.supported_options() == config.LALAL_STEM_OPTIONS
        # No "other" bucket and no lead-versus-backing model, so the demucs
        # options that promise those are not on the list.
        assert "stems" not in separator.supported_options()
        assert "karaoke" not in separator.supported_options()

    def test_it_declares_that_the_track_leaves_the_machine(self, separator):
        assert separator.uploads_audio


class TestSeparating:
    def test_it_uploads_once_and_writes_the_stem(self, separator, track, tmp_path):
        out = separator.separate(track, ["vocals"], tmp_path / "out")
        assert separator.session.uploads == 1
        assert set(out) == {"vocals"}
        assert out["vocals"].read_bytes() == b"RIFFfake"

    def test_two_halves_of_one_split_are_billed_once(
        self, separator, track, tmp_path
    ):
        # Vocals and instrumental are the two tracks one split returns.
        # Queueing it twice would charge twice for the same work.
        out = separator.separate(track, ["vocals", "instrumental"], tmp_path)
        assert len(separator.session.splits) == 1
        assert set(out) == {"vocals", "instrumental"}
        assert separator.session.downloads == [
            "https://cdn/vocals.wav", "https://cdn/back.wav",
        ]

    def test_the_same_track_is_never_uploaded_twice(
        self, separator, track, tmp_path
    ):
        separator.separate(track, ["vocals"], tmp_path)
        separator.separate(track, ["vocals"], tmp_path)
        assert separator.session.uploads == 1

    def test_the_cleanup_option_asks_for_the_cleanup(
        self, separator, track, tmp_path
    ):
        separator.separate(track, ["vocals_clean"], tmp_path)
        queued = separator.session.splits[0]
        assert queued["dereverb_enabled"] is True
        assert queued["enhanced_processing_enabled"] is True

    def test_original_alone_runs_nothing(self, separator, track, tmp_path):
        assert separator.separate(track, ["original"], tmp_path) == {}
        assert separator.session.uploads == 0

    def test_an_option_it_cannot_do_is_refused_rather_than_reinterpreted(
        self, separator, track, tmp_path
    ):
        with pytest.raises(lalal.LalalError, match="stems"):
            separator.separate(track, ["stems"], tmp_path)

    def test_progress_is_the_services_own_number(
        self, api, track, tmp_path
    ):
        engine = lalal.LalalSeparator(
            api_key="k", session=api, sleep=lambda _s: None
        )
        seen: list[float] = []
        engine.separate(
            track, ["vocals"], tmp_path,
            progress=lambda f, m="": seen.append(f),
        )
        assert seen[0] == 0.0            # uploading
        assert max(seen) <= 1.0
        assert sorted(seen) == seen      # never goes backwards

    def test_a_reported_error_is_raised_not_swallowed(self, api, track, tmp_path):
        original = api.post

        def failing_post(url, **kw):
            if url.endswith("/api/check/"):
                return FakeResponse({"status": "success", "result": {
                    "file-1": {"task": {"state": "error",
                                        "error": "unsupported audio"}},
                }})
            return original(url, **kw)

        api.post = failing_post
        engine = lalal.LalalSeparator(
            api_key="k", session=api, sleep=lambda _s: None
        )
        with pytest.raises(lalal.LalalError, match="unsupported audio"):
            engine.separate(track, ["vocals"], tmp_path)

    def test_cancelling_tells_the_service_to_stop_paying_attention(
        self, api, track, tmp_path
    ):
        engine = lalal.LalalSeparator(
            api_key="k", session=api, sleep=lambda _s: None
        )
        with pytest.raises(lalal.LalalError, match="cancelled"):
            engine.separate(
                track, ["vocals"], tmp_path, should_cancel=lambda: True
            )
        assert api.cancels == 1

    def test_a_stale_result_for_another_stem_is_not_mistaken_for_this_one(
        self, separator
    ):
        # A file id carries its most recent split. Right after queueing a
        # second stem, the check can still be showing the first one finished.
        separator.session._stem = "bass"
        separator.session._checks = 99      # "already successful"
        seen = []
        with pytest.raises(lalal.LalalError, match="did not finish"):
            separator.timeout_seconds = -1   # one pass, then give up
            separator.wait("file-1", stem="vocals",
                           progress=lambda f, m="": seen.append(f))


class TestTheKey:
    def test_limits_report_what_is_left(self, separator):
        limits = separator.limits()
        assert limits.option == "Plus"
        assert "90 of 100 minutes left" in limits.describe

    def test_reading_limits_without_a_key_is_refused(self):
        with pytest.raises(lalal.LalalUnavailable):
            lalal.LalalSeparator(api_key="").limits()


class TestChoosingTheEngine:
    def test_the_free_engine_is_the_default(self):
        assert config.DEFAULT_STEM_ENGINE == "uvr"
        assert config.stem_engine("uvr").free

    def test_lalal_is_declared_as_paid_and_names_its_service(self):
        option = config.stem_engine("lalal")
        assert option.paid
        assert option.service == "lalal"

    def test_the_local_engine_can_do_everything(self):
        assert set(config.stem_options_for("uvr")) == {
            o.key for o in config.STEM_OPTIONS
        }
