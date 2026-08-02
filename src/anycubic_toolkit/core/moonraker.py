"""Minimal Moonraker client for live printer status.

Rinkhals exposes the standard `Moonraker <https://moonraker.readthedocs.io>`_
API on port 7125, so a Rinkhals-equipped Anycubic printer can be queried over
the LAN. This client uses only stdlib HTTP (synchronous; the UI wraps calls in
worker threads) and reads a handful of well-known endpoints:

* ``/printer/info`` — state and Klipper host details.
* ``/server/info`` — Moonraker version.
* ``/printer/objects/query`` — live temperatures and print progress.

Every call is best-effort: on any failure a :class:`PrinterStatus` with
``online=False`` and an ``error`` message is returned instead of raising, so the
UI can show a clean "could not connect" state.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from anycubic_toolkit import __app_name__, __version__

_USER_AGENT = f"{__app_name__}/{__version__}"
DEFAULT_PORT = 7125


@dataclass
class PrinterStatus:
    """A snapshot of live printer state from Moonraker."""

    online: bool = False
    error: str = ""
    state: str = ""
    hostname: str = ""
    klipper_version: str = ""
    moonraker_version: str = ""
    extruder_temp: float = 0.0
    extruder_target: float = 0.0
    bed_temp: float = 0.0
    bed_target: float = 0.0
    print_state: str = ""
    print_filename: str = ""
    print_progress: float = 0.0  # 0.0 – 1.0
    print_duration: float = 0.0  # seconds


class MoonrakerClient:
    """Reads live status from a Moonraker instance over HTTP."""

    def __init__(self, host: str, port: int = DEFAULT_PORT, timeout: float = 6.0) -> None:
        self.host = (host or "").strip()
        self.port = port or DEFAULT_PORT
        self.timeout = timeout

    def base_url(self) -> str:
        """Base Moonraker URL, e.g. ``http://192.168.1.50:7125``."""
        return f"http://{self.host}:{self.port}"

    def web_url(self) -> str:
        """Best-guess web UI (Mainsail/Fluidd) URL for the printer."""
        return f"http://{self.host}"

    def fetch_status(self) -> PrinterStatus:
        """Query the printer and return a combined :class:`PrinterStatus`."""
        if not self.host:
            return PrinterStatus(online=False, error="no-host")

        info = self._get("/printer/info")
        if info is None:
            return PrinterStatus(online=False, error="unreachable")

        status = PrinterStatus(online=True)
        status.state = str(info.get("state", ""))
        status.hostname = str(info.get("hostname", ""))
        status.klipper_version = str(info.get("software_version", ""))

        server = self._get("/server/info")
        if isinstance(server, dict):
            status.moonraker_version = str(server.get("moonraker_version", ""))

        query = "/printer/objects/query?extruder&heater_bed&print_stats&display_status"
        objects = self._get(query)
        if isinstance(objects, dict):
            self._apply_objects(status, objects.get("status", {}))
        return status

    # ------------------------------------------------------------- controls

    def pause_print(self) -> bool:
        """Pause the running print. Returns True when Moonraker accepted it."""
        return self._post("/printer/print/pause")

    def resume_print(self) -> bool:
        """Resume a paused print."""
        return self._post("/printer/print/resume")

    def cancel_print(self) -> bool:
        """Cancel (stop) the running print."""
        return self._post("/printer/print/cancel")

    # ------------------------------------------------------------- internal

    @staticmethod
    def _apply_objects(status: PrinterStatus, objects: dict) -> None:
        extruder = objects.get("extruder", {})
        if isinstance(extruder, dict):
            status.extruder_temp = _as_float(extruder.get("temperature"))
            status.extruder_target = _as_float(extruder.get("target"))
        bed = objects.get("heater_bed", {})
        if isinstance(bed, dict):
            status.bed_temp = _as_float(bed.get("temperature"))
            status.bed_target = _as_float(bed.get("target"))
        stats = objects.get("print_stats", {})
        if isinstance(stats, dict):
            status.print_state = str(stats.get("state", ""))
            status.print_filename = str(stats.get("filename", ""))
            status.print_duration = _as_float(stats.get("print_duration"))
        display = objects.get("display_status", {})
        if isinstance(display, dict):
            status.print_progress = _as_float(display.get("progress"))

    def _post(self, path: str) -> bool:
        url = f"{self.base_url()}{path}"
        request = urllib.request.Request(
            url, method="POST", headers={"User-Agent": _USER_AGENT}
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return 200 <= response.status < 300
        except (urllib.error.URLError, TimeoutError, OSError):
            return False

    def _get(self, path: str) -> dict | None:
        url = f"{self.base_url()}{path}"
        request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                payload = json.loads(response.read().decode(charset, errors="replace"))
        except (urllib.error.URLError, TimeoutError, ValueError, OSError, json.JSONDecodeError):
            return None
        result = payload.get("result") if isinstance(payload, dict) else None
        return result if isinstance(result, dict) else None


def _as_float(value: object) -> float:
    try:
        return round(float(value), 1)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
