"""Print history: detect completed prints and keep a local log.

Fed a stream of :class:`~anycubic_toolkit.core.telemetry.PrinterSnapshot`
readings, this detects when a print starts and finishes and appends a compact
record to ``<data>/print_history.jsonl``. It also reports simple statistics
(count, success rate, total print time). Everything stays local.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from anycubic_toolkit.core.config import data_dir
from anycubic_toolkit.core.telemetry import (
    STATE_FAILED,
    STATE_FINISHED,
    STATE_IDLE,
    STATE_PAUSED,
    STATE_PRINTING,
    PrinterSnapshot,
)

HISTORY_FILE_NAME = "print_history.jsonl"
_MAX_RECORDS = 500


@dataclass
class PrintRecord:
    """One completed (or failed) print."""

    printer_id: str = ""
    model: str = ""
    filename: str = ""
    result: str = "finished"          # "finished" | "failed"
    started_at: str = ""              # ISO-8601 UTC
    ended_at: str = ""
    duration_seconds: int = 0
    max_progress: float = 0.0

    def duration_text(self) -> str:
        total = max(0, self.duration_seconds)
        hours, rem = divmod(total, 3600)
        minutes = rem // 60
        return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


@dataclass
class _ActiveSession:
    printer_id: str
    model: str
    filename: str
    started_at: datetime
    max_progress: float = 0.0


@dataclass
class HistoryStats:
    total: int = 0
    finished: int = 0
    failed: int = 0
    total_seconds: int = 0
    tracked_by_id: dict[str, _ActiveSession] = field(default_factory=dict)

    @property
    def success_rate(self) -> float:
        return (self.finished / self.total) if self.total else 0.0

    def total_time_text(self) -> str:
        hours = self.total_seconds // 3600
        minutes = (self.total_seconds % 3600) // 60
        return f"{hours}h {minutes:02d}m"


class PrintHistory:
    """Records completed prints from a stream of snapshots."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or (data_dir() / HISTORY_FILE_NAME)
        self._active: dict[str, _ActiveSession] = {}

    # ------------------------------------------------------------- ingestion

    def update(self, snapshot: PrinterSnapshot) -> PrintRecord | None:
        """Fold one reading in; return a :class:`PrintRecord` when one completes.

        A session opens when a printer enters the printing state and closes when
        it leaves it: finishing (or reaching ~100%) counts as ``finished``,
        anything else (error/cancel/offline mid-print) counts as ``failed``.
        """
        pid = snapshot.printer_id or snapshot.model or "printer"
        active = self._active.get(pid)

        if snapshot.state == STATE_PRINTING:
            if active is None:
                self._active[pid] = _ActiveSession(
                    printer_id=pid,
                    model=snapshot.model,
                    filename=snapshot.filename,
                    started_at=datetime.now(timezone.utc),
                    max_progress=snapshot.progress,
                )
            else:
                active.max_progress = max(active.max_progress, snapshot.progress)
                if not active.filename and snapshot.filename:
                    active.filename = snapshot.filename
            return None

        if snapshot.state == STATE_PAUSED:
            # Keep the session open across pauses.
            if active is not None:
                active.max_progress = max(active.max_progress, snapshot.progress)
            return None

        # Any non-printing, non-paused state ends an open session.
        if active is None:
            return None
        del self._active[pid]
        return self._close(active, snapshot)

    def _close(self, active: _ActiveSession, snapshot: PrinterSnapshot) -> PrintRecord:
        ended = datetime.now(timezone.utc)
        max_progress = max(active.max_progress, snapshot.progress)
        finished = snapshot.state == STATE_FINISHED or max_progress >= 0.98
        record = PrintRecord(
            printer_id=active.printer_id,
            model=active.model or snapshot.model,
            filename=active.filename or snapshot.filename,
            result="finished" if finished else "failed",
            started_at=active.started_at.isoformat(timespec="seconds"),
            ended_at=ended.isoformat(timespec="seconds"),
            duration_seconds=int((ended - active.started_at).total_seconds()),
            max_progress=round(max_progress, 3),
        )
        self._append(record)
        return record

    # ------------------------------------------------------------- storage

    def _append(self, record: PrintRecord) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
        except OSError:
            pass

    def records(self, limit: int = _MAX_RECORDS) -> list[PrintRecord]:
        """Return stored records, newest first."""
        records: list[PrintRecord] = []
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        for line in lines[-limit:]:
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                records.append(_record_from_dict(data))
        records.reverse()
        return records

    def stats(self) -> HistoryStats:
        stats = HistoryStats()
        for record in self.records():
            stats.total += 1
            if record.result == "finished":
                stats.finished += 1
            else:
                stats.failed += 1
            stats.total_seconds += max(0, record.duration_seconds)
        return stats

    def total_print_seconds(self) -> int:
        return sum(max(0, r.duration_seconds) for r in self.records())

    def clear(self) -> None:
        try:
            self._path.unlink()
        except OSError:
            pass
        self._active.clear()


def _record_from_dict(data: dict[str, Any]) -> PrintRecord:
    return PrintRecord(
        printer_id=str(data.get("printer_id", "")),
        model=str(data.get("model", "")),
        filename=str(data.get("filename", "")),
        result=str(data.get("result", "finished")),
        started_at=str(data.get("started_at", "")),
        ended_at=str(data.get("ended_at", "")),
        duration_seconds=int(data.get("duration_seconds", 0) or 0),
        max_progress=float(data.get("max_progress", 0.0) or 0.0),
    )
