"""Printer monitor: routes live snapshots to history, notifications and HA.

UI-agnostic so it can be unit-tested without Qt. The Connect page polls a
printer on a timer and calls :meth:`PrinterMonitor.ingest` with each
:class:`~anycubic_toolkit.core.telemetry.PrinterSnapshot`; the monitor updates
the print history, fires a notification when a print completes, and republishes
to Home Assistant.
"""

from __future__ import annotations

from typing import Callable

from anycubic_toolkit.core.ha_publisher import HomeAssistantPublisher
from anycubic_toolkit.core.notifications import Notifier
from anycubic_toolkit.core.print_history import PrintHistory, PrintRecord
from anycubic_toolkit.core.telemetry import PrinterSnapshot


class PrinterMonitor:
    """Dispatches each snapshot to history, notifier and the HA bridge."""

    def __init__(
        self,
        history: PrintHistory,
        notifier: Notifier | None = None,
        ha_publisher: HomeAssistantPublisher | None = None,
        on_event: Callable[[str, str], None] | None = None,
    ) -> None:
        self._history = history
        self._notifier = notifier
        self._ha = ha_publisher
        self._on_event = on_event

    def ingest(self, snapshot: PrinterSnapshot) -> PrintRecord | None:
        """Process one reading. Returns a record if a print just completed."""
        record = self._history.update(snapshot)
        if record is not None:
            self._announce_completion(record)
        if self._ha is not None and self._ha.config.is_ready() and snapshot.online:
            self._ha.publish(snapshot)
        return record

    def _announce_completion(self, record: PrintRecord) -> None:
        finished = record.result == "finished"
        title = (
            "Print finished \N{WHITE HEAVY CHECK MARK}"
            if finished
            else "Print failed \N{CROSS MARK}"
        )
        name = record.filename or record.model or "Print"
        message = f"{name} — {record.duration_text()}"
        if self._notifier is not None and self._notifier.is_ready():
            self._notifier.notify(title, message)
        if self._on_event is not None:
            self._on_event(title, message)
