"""JobQueue -> Qt signals.

core/jobs.py is deliberately Qt-free so the state machine unit-tests without a
display. This is the only adapter between the two, and it exists because job
callbacks fire on worker threads while Qt widgets may only be touched from the
GUI thread.

The crossing is done by emitting from a QObject that lives on the GUI thread:
a signal emitted from another thread to a receiver with GUI-thread affinity is
delivered as a queued connection, which is the one thread-safe path Qt offers.
Nothing here touches a widget directly.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Qt, Signal

from ..core.jobs import JobQueue, JobSnapshot


class JobBridge(QObject):
    """Re-emits queue events on the GUI thread.

    Connect to `changed` for a single stream of everything, or to the specific
    signals when only one transition matters.
    """

    submitted = Signal(object)
    started = Signal(object)
    progressed = Signal(object)
    succeeded = Signal(object)
    failed = Signal(object)
    cancelled = Signal(object)
    retrying = Signal(object)
    #: Every event, after the specific one. (event_name, snapshot)
    changed = Signal(str, object)

    _relay = Signal(str, object)

    def __init__(self, queue: JobQueue, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.queue = queue

        # The hop that makes this safe: _relay is emitted from whichever
        # worker thread ran the job, and _dispatch has this object's (GUI)
        # thread affinity, so Qt queues the call rather than running it on the
        # worker.
        self._relay.connect(self._dispatch, Qt.QueuedConnection)
        self._unsubscribe = queue.subscribe(self._on_event)

    def _on_event(self, event: str, snapshot: JobSnapshot) -> None:
        """Called on a worker thread. Must do nothing but forward."""
        self._relay.emit(event, snapshot)

    def _dispatch(self, event: str, snapshot: JobSnapshot) -> None:
        """Called on the GUI thread."""
        signal = {
            "submitted": self.submitted,
            "started": self.started,
            "progress": self.progressed,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "cancelled": self.cancelled,
            "retrying": self.retrying,
        }.get(event)
        if signal is not None:
            signal.emit(snapshot)
        self.changed.emit(event, snapshot)

    def close(self) -> None:
        """Stop relaying. Called before the queue shuts down so a late event
        cannot reach a half-torn-down widget."""
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
