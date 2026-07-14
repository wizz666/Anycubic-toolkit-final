"""Background execution helpers.

All network and file-system heavy work runs off the GUI thread through
:class:`FunctionWorker`, a thin QRunnable wrapper around a plain callable.

Usage::

    worker = FunctionWorker(api.get_news)
    worker.signals.finished.connect(self._on_news)
    worker.signals.error.connect(self._on_error)
    run_in_background(worker)
"""

from __future__ import annotations

import threading
import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot


class WorkerSignals(QObject):
    """Signals emitted by :class:`FunctionWorker`."""

    finished = Signal(object)   # result of the callable
    error = Signal(str)         # human-readable error message
    progress = Signal(int, str)  # percentage, task description


class FunctionWorker(QRunnable):
    """Run any callable on the global thread pool and report the result.

    If the callable accepts a ``progress`` keyword argument, it receives a
    ``Callable[[int, str], None]`` it can use to report progress.
    """

    def __init__(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:  # noqa: D102 - QRunnable interface
        try:
            if _accepts_progress(self.fn):
                self.kwargs.setdefault("progress", self.signals.progress.emit)
            result = self.fn(*self.args, **self.kwargs)
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            traceback.print_exc()
            self.signals.error.emit(str(exc))
        else:
            self.signals.finished.emit(result)


def _accepts_progress(fn: Callable[..., Any]) -> bool:
    """Check whether *fn* declares a ``progress`` parameter."""
    try:
        import inspect

        return "progress" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


_active_workers: set[FunctionWorker] = set()
_active_lock = threading.Lock()


def run_in_background(worker: FunctionWorker) -> None:
    """Submit a worker to the global thread pool.

    A ``QRunnable`` has no owner on the Python side once the caller's local
    variable goes out of scope, so nothing keeps it (or its ``signals``
    QObject) alive while it runs on the pool thread — Python can garbage
    collect it mid-run, and the eventual ``signals.finished.emit(...)`` then
    raises "Signal source has been deleted". This is rare with a single
    in-flight worker but reliably reproducible with several concurrent ones
    (e.g. polling multiple printers at once), so a strong reference is held
    here until the worker reports its result.
    """
    with _active_lock:
        _active_workers.add(worker)
    worker.signals.finished.connect(lambda _result: _release(worker))
    worker.signals.error.connect(lambda _message: _release(worker))
    QThreadPool.globalInstance().start(worker)


def _release(worker: FunctionWorker) -> None:
    with _active_lock:
        _active_workers.discard(worker)
