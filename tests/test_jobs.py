"""Job queue state machine: progress, cancel mid-flight, retry, isolation."""

from __future__ import annotations

import threading
import time

import pytest

from neyta.core.jobs import JobCancelled, JobQueue, JobState


def noop(ctx):
    return "ok"


def boom(ctx):
    raise RuntimeError("nope")


class TestHappyPath:
    def test_a_job_runs_and_reports_its_result(self, queue):
        jid = queue.submit(lambda ctx: 6 * 7, kind="test")
        snap = queue.wait(jid, timeout=5)
        assert snap.state is JobState.SUCCEEDED
        assert snap.result == 42
        assert snap.error is None

    def test_success_forces_progress_to_one(self, queue):
        jid = queue.submit(lambda ctx: ctx.progress(0.3) or "done")
        assert queue.wait(jid, timeout=5).progress == 1.0

    def test_ids_are_distinct_and_increasing(self, queue):
        ids = [queue.submit(noop) for _ in range(5)]
        assert ids == sorted(set(ids))
        queue.wait_all(timeout=5)

    def test_label_defaults_to_kind(self, queue):
        jid = queue.submit(noop, kind="search")
        assert queue.get(jid).label == "search"

    def test_duration_is_recorded(self, queue):
        jid = queue.submit(lambda ctx: time.sleep(0.02))
        snap = queue.wait(jid, timeout=5)
        assert snap.duration is not None and snap.duration > 0

    def test_unknown_job_id_is_none(self, queue):
        assert queue.get(9999) is None
        assert queue.wait(9999, timeout=0.1) is None


class TestProgress:
    def test_progress_reaches_subscribers(self, queue):
        seen = []
        queue.subscribe(lambda ev, s: seen.append((ev, s.progress)) if ev == "progress" else None)

        def work(ctx):
            for i in range(1, 5):
                ctx.progress(i / 4, f"step {i}")
            return None

        queue.wait(queue.submit(work), timeout=5)
        assert [p for _, p in seen] == [0.25, 0.5, 0.75, 1.0]

    def test_progress_is_clamped(self, queue):
        def work(ctx):
            ctx.progress(-5)
            assert queue.get(ctx.job_id).progress == 0.0
            ctx.progress(99)
            assert queue.get(ctx.job_id).progress == 1.0

        assert queue.wait(queue.submit(work), timeout=5).state is JobState.SUCCEEDED

    def test_message_persists_until_replaced(self, queue):
        def work(ctx):
            ctx.progress(0.5, "downloading")
            ctx.progress(0.6)  # no message
            return queue.get(ctx.job_id).message

        assert queue.wait(queue.submit(work), timeout=5).result == "downloading"


class TestFailure:
    def test_failure_is_captured_not_raised(self, queue):
        snap = queue.wait(queue.submit(boom), timeout=5)
        assert snap.state is JobState.FAILED
        assert isinstance(snap.error, RuntimeError)

    def test_one_failure_does_not_affect_siblings(self, queue):
        bad = [queue.submit(boom) for _ in range(3)]
        good = [queue.submit(lambda ctx, n=n: n) for n in range(5)]
        queue.wait_all(timeout=10)

        assert all(queue.get(j).state is JobState.FAILED for j in bad)
        assert [queue.get(j).result for j in good] == [0, 1, 2, 3, 4]

    def test_the_pool_survives_a_burst_of_failures(self, queue):
        for _ in range(20):
            queue.submit(boom)
        queue.wait_all(timeout=10)
        assert queue.wait(queue.submit(noop), timeout=5).result == "ok"

    def test_a_raising_subscriber_does_not_break_the_queue(self, queue):
        def bad_subscriber(ev, snap):
            raise ValueError("subscriber is broken")

        queue.subscribe(bad_subscriber)
        assert queue.wait(queue.submit(noop), timeout=5).state is JobState.SUCCEEDED


class TestRetry:
    def test_a_flaky_job_succeeds_on_retry(self, queue):
        attempts = []

        def flaky(ctx):
            attempts.append(ctx.attempt)
            if len(attempts) < 3:
                raise OSError("peer vanished")
            return "recovered"

        snap = queue.wait(queue.submit(flaky, max_retries=3), timeout=5)
        assert snap.state is JobState.SUCCEEDED
        assert snap.result == "recovered"
        assert attempts == [1, 2, 3]

    def test_retries_are_bounded(self, queue):
        calls = []

        def always_fails(ctx):
            calls.append(1)
            raise OSError

        snap = queue.wait(queue.submit(always_fails, max_retries=2), timeout=5)
        assert snap.state is JobState.FAILED
        assert len(calls) == 3  # first attempt + 2 retries

    def test_no_retries_by_default(self, queue):
        calls = []
        queue.wait(queue.submit(lambda ctx: calls.append(1) or boom(ctx)), timeout=5)
        assert len(calls) == 1

    def test_retrying_is_announced(self, queue):
        events = []
        queue.subscribe(lambda ev, s: events.append(ev))
        state = {"n": 0}

        def flaky(ctx):
            state["n"] += 1
            if state["n"] == 1:
                raise OSError
            return "ok"

        queue.wait(queue.submit(flaky, max_retries=1), timeout=5)
        assert "retrying" in events


class TestCancellation:
    def test_a_running_job_stops_at_its_next_check(self, queue):
        started = threading.Event()
        released = threading.Event()

        def work(ctx):
            started.set()
            for _ in range(500):
                ctx.check_cancelled()
                if released.wait(0.01):
                    pass
            return "should not get here"

        jid = queue.submit(work)
        assert started.wait(5)
        assert queue.cancel(jid) is True
        snap = queue.wait(jid, timeout=5)
        assert snap.state is JobState.CANCELLED
        assert snap.result is None

    def test_cancellation_is_not_a_failure(self, queue):
        started = threading.Event()

        def work(ctx):
            started.set()
            while True:
                ctx.check_cancelled()
                time.sleep(0.01)

        jid = queue.submit(work)
        started.wait(5)
        queue.cancel(jid)
        snap = queue.wait(jid, timeout=5)
        assert snap.state is JobState.CANCELLED
        assert snap.error is None

    def test_ctx_cancelled_flag_allows_graceful_cleanup(self, queue):
        started = threading.Event()
        cleaned = threading.Event()

        def work(ctx):
            started.set()
            while not ctx.cancelled:
                time.sleep(0.01)
            cleaned.set()
            raise JobCancelled

        jid = queue.submit(work)
        started.wait(5)
        queue.cancel(jid)
        queue.wait(jid, timeout=5)
        assert cleaned.is_set()

    def test_a_pending_job_is_cancelled_before_it_runs(self):
        q = JobQueue(workers=1)
        try:
            gate = threading.Event()
            q.submit(lambda ctx: gate.wait(5))
            time.sleep(0.05)  # let the single worker pick up the blocker
            queued = q.submit(lambda ctx: pytest.fail("must not run"))
            assert q.cancel(queued) is True
            gate.set()
            assert q.wait(queued, timeout=5).state is JobState.CANCELLED
        finally:
            q.shutdown()

    def test_cancelling_a_finished_job_is_a_no_op(self, queue):
        jid = queue.submit(noop)
        queue.wait(jid, timeout=5)
        assert queue.cancel(jid) is False
        assert queue.get(jid).state is JobState.SUCCEEDED

    def test_cancelling_an_unknown_job_is_false(self, queue):
        assert queue.cancel(4242) is False

    def test_cancel_during_retry_backoff_stops_retrying(self, queue):
        calls = []
        started = threading.Event()

        def flaky(ctx):
            calls.append(1)
            started.set()
            raise OSError

        jid = queue.submit(flaky, max_retries=5)
        started.wait(5)
        queue.cancel(jid)
        snap = queue.wait(jid, timeout=5)
        assert snap.state in (JobState.CANCELLED, JobState.FAILED)

    def test_cancel_all_clears_the_queue(self):
        q = JobQueue(workers=1)
        try:
            gate = threading.Event()
            q.submit(lambda ctx: gate.wait(0.2))
            for _ in range(5):
                q.submit(lambda ctx: pytest.fail("must not run"))
            time.sleep(0.05)
            q.cancel_all()
            gate.set()
            q.wait_all(timeout=5)
            assert not q.active()
        finally:
            q.shutdown()


class TestEvents:
    def test_lifecycle_events_arrive_in_order(self, queue):
        events = []
        queue.subscribe(lambda ev, s: events.append(ev))
        queue.wait(queue.submit(noop), timeout=5)
        assert events[0] == "submitted"
        assert "started" in events
        assert events[-1] == "succeeded"

    def test_failure_emits_failed(self, queue):
        events = []
        queue.subscribe(lambda ev, s: events.append(ev))
        queue.wait(queue.submit(boom), timeout=5)
        assert events[-1] == "failed"

    def test_unsubscribe_stops_delivery(self, queue):
        events = []
        off = queue.subscribe(lambda ev, s: events.append(ev))
        queue.wait(queue.submit(noop), timeout=5)
        count = len(events)
        off()
        queue.wait(queue.submit(noop), timeout=5)
        assert len(events) == count

    def test_snapshots_are_immutable(self, queue):
        snap = queue.wait(queue.submit(noop), timeout=5)
        with pytest.raises(Exception):
            snap.state = JobState.PENDING


class TestQueueLifecycle:
    def test_terminal_states_are_terminal(self):
        assert JobState.SUCCEEDED.terminal
        assert JobState.FAILED.terminal
        assert JobState.CANCELLED.terminal
        assert not JobState.PENDING.terminal
        assert not JobState.RUNNING.terminal

    def test_active_excludes_finished_jobs(self, queue):
        queue.wait(queue.submit(noop), timeout=5)
        assert queue.active() == []

    def test_submitting_after_shutdown_raises(self):
        q = JobQueue(workers=1)
        q.shutdown()
        with pytest.raises(RuntimeError):
            q.submit(noop)

    def test_workers_must_be_positive(self):
        with pytest.raises(ValueError):
            JobQueue(workers=0)

    def test_context_manager_shuts_down(self):
        with JobQueue(workers=1) as q:
            q.wait(q.submit(noop), timeout=5)
        with pytest.raises(RuntimeError):
            q.submit(noop)

    def test_concurrency_is_bounded_by_worker_count(self):
        q = JobQueue(workers=2)
        try:
            live = []
            peak = []
            lock = threading.Lock()

            def work(ctx):
                with lock:
                    live.append(1)
                    peak.append(len(live))
                time.sleep(0.05)
                with lock:
                    live.pop()

            for _ in range(8):
                q.submit(work)
            q.wait_all(timeout=10)
            assert max(peak) <= 2
        finally:
            q.shutdown()
