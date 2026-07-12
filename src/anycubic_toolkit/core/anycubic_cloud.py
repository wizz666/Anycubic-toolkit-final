"""Anycubic Cloud (optional, opt-in): read-only printer status.

This module talks to Anycubic's **unofficial** cloud API — the same one the
Anycubic apps and Slicer Next use. It exists for one scenario only: checking
your printer when you are *away* from your home network. It is disabled by
default and must be enabled explicitly in Settings; the local paths (Moonraker
and Anycubic LAN mode) are always preferred.

Design constraints, deliberately:

* **Read-only.** Only status is fetched. No remote print control — a
  misfired command against an undocumented API has physical consequences,
  while reading status cannot damage anything.
* **Clean-room.** The mature community implementations are GPL-3.0 and no code
  is reused from them; this implementation is written from publicly documented
  protocol facts (endpoints, header names, token source) and stays MIT.
* **Honest fragility.** Anycubic can change this API at any time. Failures
  surface as a readable error, never a crash.

Authentication uses the access token that Anycubic Slicer Next stores locally
after login (``AnycubicSlicerNext.conf`` → ``access_token``); the app can
auto-detect it or the user pastes it manually. The token is exchanged for a
session token which is then sent as the ``XX-Token`` header.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from anycubic_toolkit import __app_name__, __version__

# Public protocol facts (see module docstring).
CLOUD_API_ROOT = "https://cloud-universe.anycubic.com/p/p/workbench/api"
_AUTH_ORIGIN = "https://uc.makeronline.com"
_LOGIN_PATH = "/v3/public/loginWithAccessToken"
_PRINTERS_PATH = "/v2/printer/all"

_TIMEOUT = 15
_USER_AGENT = f"{__app_name__}/{__version__}"


class CloudError(Exception):
    """Readable failure talking to the Anycubic cloud."""


# ------------------------------------------------------------------ token


def find_slicer_token() -> str:
    """Best-effort: read the access token Anycubic Slicer Next stored locally.

    Returns an empty string when no logged-in Slicer Next config is found.
    Nothing is transmitted anywhere by this function; it only reads the local
    configuration file.
    """
    for config_path in _slicer_config_candidates():
        try:
            raw = config_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        token = _dig_for_token(data)
        if token:
            return token
    return ""


def _slicer_config_candidates() -> list[Path]:
    candidates: list[Path] = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            candidates.append(
                Path(appdata) / "AnycubicSlicerNext" / "AnycubicSlicerNext.conf"
            )
    elif sys.platform == "darwin":
        candidates.append(
            Path.home()
            / "Library"
            / "Application Support"
            / "AnycubicSlicerNext"
            / "AnycubicSlicerNext.conf"
        )
    else:
        candidates.append(
            Path.home() / ".config" / "AnycubicSlicerNext" / "AnycubicSlicerNext.conf"
        )
    return candidates


def _dig_for_token(data: Any) -> str:
    """Find an ``access_token`` string anywhere in a config structure."""
    if isinstance(data, dict):
        value = data.get("access_token")
        if isinstance(value, str) and len(value) >= 32:
            return value
        for child in data.values():
            found = _dig_for_token(child)
            if found:
                return found
    elif isinstance(data, list):
        for child in data:
            found = _dig_for_token(child)
            if found:
                return found
    return ""


# ------------------------------------------------------------------ client


@dataclass
class CloudPrinter:
    """One printer as reported by the cloud (read-only snapshot)."""

    name: str = ""
    model: str = ""
    online: bool = False
    is_printing: bool = False
    print_state: str = ""
    progress: float = 0.0  # 0.0 – 1.0
    filename: str = ""
    nozzle_temp: float = 0.0
    bed_temp: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)


class AnycubicCloudClient:
    """Read-only status client for the (unofficial) Anycubic cloud."""

    def __init__(self, access_token: str, api_root: str = CLOUD_API_ROOT) -> None:
        self._access_token = (access_token or "").strip()
        self._api_root = api_root.rstrip("/")
        self._session_token = ""

    def login(self) -> None:
        """Exchange the Slicer access token for a session token."""
        if not self._access_token:
            raise CloudError("no-token")
        payload = urllib.parse.urlencode(
            {"device_type": "pcf", "access_token": self._access_token}
        ).encode()
        response = self._request(_LOGIN_PATH, method="POST", body=payload)
        data = response.get("data")
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            message = str(response.get("msg") or "login rejected")
            raise CloudError(f"auth: {message}")
        self._session_token = token

    def printers(self) -> list[CloudPrinter]:
        """All printers on the account, with their current status."""
        if not self._session_token:
            self.login()
        response = self._request(_PRINTERS_PATH)
        data = response.get("data")
        if isinstance(data, dict):
            data = data.get("printers", data.get("list", []))
        if not isinstance(data, list):
            return []
        return [_parse_printer(entry) for entry in data if isinstance(entry, dict)]

    # ------------------------------------------------------------- internal

    def _request(self, path: str, method: str = "GET", body: bytes | None = None) -> dict:
        headers = {
            "User-Agent": _USER_AGENT,
            "Origin": _AUTH_ORIGIN,
            "XX-LANGUAGE": "US",
        }
        if self._session_token:
            headers["XX-Token"] = self._session_token
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = urllib.request.Request(
            f"{self._api_root}{path}", method=method, data=body, headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                payload = json.loads(response.read().decode(charset, errors="replace"))
        except urllib.error.HTTPError as exc:
            raise CloudError(f"http {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise CloudError(f"unreachable: {exc}") from exc
        except (json.JSONDecodeError, ValueError) as exc:
            raise CloudError("invalid response") from exc
        if not isinstance(payload, dict):
            raise CloudError("unexpected response")
        return payload


def _parse_printer(entry: dict[str, Any]) -> CloudPrinter:
    """Tolerantly map a cloud printer object to a snapshot."""

    def pick(*keys: str) -> Any:
        for key in keys:
            value = entry.get(key)
            if value not in (None, ""):
                return value
        return None

    status_text = str(pick("device_status", "status", "printer_status") or "")
    state = str(pick("print_status", "work_status", "state") or status_text)
    online_raw = pick("is_online", "online")
    online = (
        bool(online_raw)
        if online_raw is not None
        else status_text.lower() not in ("", "offline", "0")
    )

    progress_raw = pick("progress", "print_progress")
    progress = 0.0
    if progress_raw is not None:
        try:
            progress = float(progress_raw)
            progress = progress / 100.0 if progress > 1.0 else progress
        except (TypeError, ValueError):
            progress = 0.0

    def temp(*keys: str) -> float:
        value = pick(*keys)
        try:
            return round(float(value), 1)
        except (TypeError, ValueError):
            return 0.0

    printing_tokens = ("print", "busy", "working", "1")
    is_printing = any(token in state.lower() for token in printing_tokens) and (
        "finish" not in state.lower()
    )

    return CloudPrinter(
        name=str(pick("name", "printer_name", "device_name") or ""),
        model=str(pick("model", "machine_name", "model_name") or ""),
        online=online,
        is_printing=is_printing,
        print_state=state,
        progress=progress,
        filename=str(pick("filename", "gcode_name", "task_name", "print_name") or ""),
        nozzle_temp=temp("nozzle_temp", "curr_nozzle_temp", "temp_nozzle"),
        bed_temp=temp("hotbed_temp", "curr_hotbed_temp", "temp_hotbed"),
        raw=entry,
    )
