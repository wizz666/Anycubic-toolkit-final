"""Unified printer telemetry.

The three connection paths (Moonraker, Anycubic LAN mode, Anycubic cloud) each
return their own status object with slightly different field names. This module
normalizes them into a single :class:`PrinterSnapshot` so the higher-level
features — print history, notifications and the Home Assistant bridge — can all
consume one shape regardless of how the data was obtained.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid import cycles at runtime
    from anycubic_toolkit.core.anycubic_cloud import CloudPrinter
    from anycubic_toolkit.core.anycubic_lan import LanPrinterStatus
    from anycubic_toolkit.core.moonraker import PrinterStatus

# Canonical states used everywhere downstream.
STATE_PRINTING = "printing"
STATE_PAUSED = "paused"
STATE_FINISHED = "finished"
STATE_FAILED = "failed"
STATE_IDLE = "idle"
STATE_OFFLINE = "offline"


def normalize_state(raw: str, *, online: bool = True) -> str:
    """Map a printer-reported state string to a canonical state."""
    if not online:
        return STATE_OFFLINE
    text = (raw or "").strip().lower()
    if not text:
        return STATE_IDLE
    if any(t in text for t in ("cancel", "abort", "stopp", "stop", "error", "fault", "fail")):
        return STATE_FAILED
    if any(t in text for t in ("finish", "complet", "done", "success")):
        return STATE_FINISHED
    if "paus" in text:
        return STATE_PAUSED
    if any(t in text for t in ("print", "busy", "working", "run")):
        return STATE_PRINTING
    return STATE_IDLE


@dataclass
class PrinterSnapshot:
    """A single normalized reading of a printer's live state."""

    online: bool = False
    source: str = ""           # "moonraker" | "lan" | "cloud"
    printer_id: str = ""       # stable id for history/HA (host or device id)
    model: str = ""
    state: str = STATE_OFFLINE  # canonical (see STATE_* constants)
    raw_state: str = ""
    filename: str = ""
    progress: float = 0.0       # 0.0 – 1.0
    nozzle_temp: float = 0.0
    nozzle_target: float = 0.0
    bed_temp: float = 0.0
    bed_target: float = 0.0
    current_layer: int = 0
    total_layers: int = 0
    remaining_minutes: int = 0
    fan_pct: int = 0
    firmware: str = ""

    @property
    def is_printing(self) -> bool:
        return self.state == STATE_PRINTING

    @property
    def progress_pct(self) -> int:
        return max(0, min(100, round(self.progress * 100)))


def from_moonraker(status: "PrinterStatus", printer_id: str) -> PrinterSnapshot:
    return PrinterSnapshot(
        online=status.online,
        source="moonraker",
        printer_id=printer_id,
        model=status.hostname or "",
        state=normalize_state(status.print_state or status.state, online=status.online),
        raw_state=status.print_state or status.state,
        filename=status.print_filename,
        progress=status.print_progress,
        nozzle_temp=status.extruder_temp,
        nozzle_target=status.extruder_target,
        bed_temp=status.bed_temp,
        bed_target=status.bed_target,
        firmware=status.klipper_version,
    )


def from_lan(status: "LanPrinterStatus", printer_id: str) -> PrinterSnapshot:
    return PrinterSnapshot(
        online=status.online,
        source="lan",
        printer_id=printer_id,
        model=status.model_name or status.device_name,
        state=normalize_state(status.print_state, online=status.online),
        raw_state=status.print_state,
        filename=status.print_filename,
        progress=status.print_progress,
        nozzle_temp=status.nozzle_temp,
        nozzle_target=status.nozzle_target,
        bed_temp=status.bed_temp,
        bed_target=status.bed_target,
        current_layer=status.current_layer,
        total_layers=status.total_layers,
        remaining_minutes=status.remaining_minutes,
        fan_pct=status.fan_speed_pct,
        firmware=status.firmware_version,
    )


def from_cloud(printer: "CloudPrinter", printer_id: str) -> PrinterSnapshot:
    return PrinterSnapshot(
        online=printer.online,
        source="cloud",
        printer_id=printer_id or printer.name or printer.model,
        model=printer.model or printer.name,
        state=normalize_state(printer.print_state, online=printer.online),
        raw_state=printer.print_state,
        filename=printer.filename,
        progress=printer.progress,
        nozzle_temp=printer.nozzle_temp,
        bed_temp=printer.bed_temp,
    )
