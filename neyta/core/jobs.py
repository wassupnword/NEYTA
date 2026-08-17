"""The background job queue.

Non-negotiable per build plan 3.2: Soulseek searches take 15-30s by protocol
design and BS-Roformer runs minutes per track, so the UI thread never does I/O.
Searches, caption fetches, downloads, separations and conversions are all jobs.

Deliberately Qt-free. The UI adapts it with a signal bridge in neyta/ui, which
keeps the whole state machine unit-testable without a display.
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Callable, Iterable, Protocol

log = logging.getLogger(__name__)


class JobState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED)


class JobCancelled(Exception):
    """Raised inside a worker when its job was cancelled mid-flight.

    Job functions do not need to catch this; the queue treats it as the
    cancellation completing rather than as a failure.
    """


@dataclass(frozen=True)
class JobSnapshot:
    """An immutable view handed to subscribers and returned by `jobs()`.

    Subscribers run on worker threads. Giving them a live object would invite
    exactly the race the lock exists to prevent.
    """

    id: int
    kind: str
    label: str
    state: JobState
    progress: float
    message: str
    attempt: int
    max_retries: int
    result: Any = None
    error: BaseException | None = None
    submitted_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None

    @property
    def duration(self) -> float | None:
        if self.started_at is None:
            return None
        return (self.finished_at or time.monotonic()) - self.started_at


@dataclass
class _Job:
    id: int
    kind: str
    label: str
    fn: "JobFn"
    max_retries: int
    state: JobState = JobState.PENDING
    progress: float = 0.0
    message: str = ""
    attempt: int = 0
    result: Any = None
    error: BaseException | None = None
    submitted_at: float = field(default_factory=time.monotonic)
    started_at: float | None = None
    finished_at: float | None = None
    cancel_requested: bool = False
    future: Future | None = None
    done: threading.Event = field(default_factory=threading.Event)

    def snapshot(self) -> JobSnapshot:
        return JobSnapshot(
            id=self.id,
            kind=self.kind,
            label=self.label,
            state=self.state,
            progress=self.progress,
            message=self.message,
            attempt=self.attempt,
            max_retries=self.max_retries,
            result=self.result,
            error=self.error,
            submitted_at=self.submitted_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
        )


class JobContext(Protocol):
    """What a job function receives as its first argument."""

    @property
    def cancelled(self) -> bool: ...

    def check_cancelled(self) -> None: ...

    def progress(self, fraction: float, message: str = "") -> None: ...


JobFn = Callable[[JobContext], Any]
Subscriber = Callable[[str, JobSnapshot], None]


class _Context:
    def __init__(self, queue: "JobQueue", job: _Job) -> None:
        self._queue = queue
        self._job = job

    @property
    def job_id(self) -> int:
        return self._job.id

    @property
    def attempt(self) -> int:
        return self._job.attempt

    @property
    def cancelled(self) -> bool:
        with self._queue._lock:
            return self._job.cancel_requested

    def check_cancelled(self) -> None:
        """Cooperative cancellation point. Call it in every loop that can run
        for more than about a second."""
        if self.cancelled:
            raise JobCancelled(f"job {self._job.id} cancelled")

    def progress(self, fraction: float, message: str = "") -> None:
        fraction = 0.0 if fraction < 0.0 else 1.0 if fraction > 1.0 else float(fraction)
        with self._queue._lock:
            if self._job.state is not JobState.RUNNING:
                return
            self._job.progress = fraction
            if message:
                self._job.message = message
            snap = self._job.snapshot()
        self._queue._emit("progress", snap)


class JobQueue:
    """Bounded pool of workers with progress, cancellation and retry.

    A failing job never affects its siblings: every worker body is wrapped, and
    a subscriber that raises is logged rather than allowed to take the queue
    down with it.
    """

    def __init__(self, workers: int = 4, *, name: str = "neyta-job") -> None:
        if workers < 1:
            raise ValueError("workers must be >= 1")
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=name)
        self._lock = threading.RLock()
        self._jobs: dict[int, _Job] = {}
        self._subscribers: list[Subscriber] = []
        self._ids = itertools.count(1)
        self._shutdown = False
        self.retry_delay: float = 0.0  # tests set this to 0; real use backs off

    # -- submission ------------------------------------------------------

    def submit(
        self,
        fn: JobFn,
        *,
        kind: str = "task",
        label: str = "",
        max_retries: int = 0,
    ) -> int:
        """Queue `fn`, which will be called with a JobContext. Returns the id."""
        with self._lock:
            if self._shutdown:
                raise RuntimeError("queue is shut down")
            job = _Job(
                id=next(self._ids),
                kind=kind,
                label=label or kind,
                fn=fn,
                max_retries=max(0, max_retries),
            )
            self._jobs[job.id] = job
            snap = job.snapshot()
        self._emit("submitted", snap)

        with self._lock:
            job.future = self._pool.submit(self._run, job)
        return job.id

    # -- inspection ------------------------------------------------------

    def get(self, job_id: int) -> JobSnapshot | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.snapshot() if job else None

    def jobs(self) -> list[JobSnapshot]:
        with self._lock:
            return [j.snapshot() for j in self._jobs.values()]

    def active(self) -> list[JobSnapshot]:
        return [j for j in self.jobs() if not j.state.terminal]

    def subscribe(self, callback: Subscriber) -> Callable[[], None]:
        """Register an observer. Returns a function that unregisters it.

        Callbacks fire on worker threads and must be cheap and non-blocking;
        the Qt bridge queues them onto the GUI thread.
        """
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    # -- control ---------------------------------------------------------

    def cancel(self, job_id: int) -> bool:
        """Request cancellation. True if the job will not (or no longer will)
        complete normally.

        A pending job is cancelled outright. A running job is flagged and stops
        at its next `check_cancelled()`, so a job that never checks will run to
        completion — that is the job function's bug to fix, not something the
        queue can force without killing threads.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.state.terminal:
                return False
            job.cancel_requested = True
            if job.state is JobState.PENDING and job.future is not None:
                if not job.future.cancel():
                    return True  # already picked up; it will stop cooperatively
            else:
                return True
        self._settle(job, JobState.CANCELLED, "cancelled")
        return True

    def cancel_all(self) -> int:
        return sum(1 for j in self.jobs() if not j.state.terminal and self.cancel(j.id))

    def wait(self, job_id: int, timeout: float | None = None) -> JobSnapshot | None:
        """Block until a job reaches a terminal state. For tests and the CLI —
        the UI subscribes instead."""
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return None
        job.done.wait(timeout)
        return job.snapshot()

    def wait_all(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        for job in list(self._jobs.values()):
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            if not job.done.wait(remaining):
                return False
        return True

    def shutdown(self, *, wait: bool = True, cancel: bool = True) -> None:
        with self._lock:
            self._shutdown = True
        if cancel:
            self.cancel_all()
        self._pool.shutdown(wait=wait)

    def __enter__(self) -> "JobQueue":
        return self

    def __exit__(self, *exc: object) -> None:
        self.shutdown()

    # -- internals -------------------------------------------------------

    def _run(self, job: _Job) -> None:
        while True:
            with self._lock:
                if job.cancel_requested:
                    cancelled_before_start = True
                else:
                    cancelled_before_start = False
                    job.attempt += 1
                    job.state = JobState.RUNNING
                    job.progress = 0.0
                    if job.started_at is None:
                        job.started_at = time.monotonic()
                    snap = job.snapshot()
            if cancelled_before_start:
                self._settle(job, JobState.CANCELLED, "cancelled")
                return
            self._emit("started", snap)

            ctx = _Context(self, job)
            try:
                result = job.fn(ctx)
            except JobCancelled:
                self._settle(job, JobState.CANCELLED, "cancelled")
                return
            except BaseException as exc:  # noqa: BLE001 — isolation is the point
                with self._lock:
                    cancelled = job.cancel_requested
                    retries_left = job.max_retries - (job.attempt - 1)
                    job.error = exc
                if cancelled:
                    self._settle(job, JobState.CANCELLED, "cancelled")
                    return
                if retries_left > 0:
                    with self._lock:
                        job.message = f"retrying after {type(exc).__name__}"
                        snap = job.snapshot()
                    log.warning(
                        "job %s (%s) failed on attempt %s, retrying: %r",
                        job.id, job.kind, job.attempt, exc,
                    )
                    self._emit("retrying", snap)
                    if self.retry_delay:
                        time.sleep(self.retry_delay * job.attempt)
                    continue
                log.error("job %s (%s) failed: %r", job.id, job.kind, exc)
                self._settle(job, JobState.FAILED, "failed")
                return
            else:
                with self._lock:
                    job.result = result
                    job.error = None
                    job.progress = 1.0
                self._settle(job, JobState.SUCCEEDED, "succeeded")
                return

    def _settle(self, job: _Job, state: JobState, event: str) -> None:
        """Move a job to its terminal state, tell everyone, then release
        waiters — in that order.

        `done` is set last on purpose. Releasing waiters first leaves a window
        where `wait()` has returned but the terminal event has not been
        delivered, so a caller that waits and then inspects what its subscriber
        saw races the emit. That window is small enough to pass on a warm
        machine and fail on a cold one.
        """
        with self._lock:
            if job.state.terminal:
                return
            job.state = state
            job.finished_at = time.monotonic()
            snap = job.snapshot()
        self._emit(event, snap)
        job.done.set()

    def _emit(self, event: str, snap: JobSnapshot) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for cb in subscribers:
            try:
                cb(event, snap)
            except Exception:  # noqa: BLE001 — a bad observer is not a bad queue
                log.exception("job subscriber raised on %s event", event)


def collect(queue: JobQueue, job_ids: Iterable[int], timeout: float | None = None):
    """Wait on several jobs and return their snapshots in submission order."""
    return [queue.wait(jid, timeout) for jid in job_ids]
