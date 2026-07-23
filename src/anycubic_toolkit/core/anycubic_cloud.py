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

Every call, including the token exchange itself, must also carry a set of
``Xx-*`` request-signing headers (app identity, a timestamp, a random nonce,
and an MD5 digest tying them together) or the API rejects it with a "missing
mandatory public parameter" error. These are not secrets — the app id/version
identify Slicer Next as the calling client, the same way a User-Agent would,
and the signature just proves the request wasn't tampered with in transit.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from anycubic_toolkit import __app_name__, __version__

# Public protocol facts (see module docstring).
CLOUD_API_ROOT = "https://cloud-universe.anycubic.com/p/p/workbench/api"
_AUTH_ORIGIN = "https://uc.makeronline.com"
_LOGIN_PATH = "/v3/public/loginWithAccessToken"
_PRINTERS_PATH = "/work/printer/getPrinters"
_PROJECTS_PATH = "/work/project/getProjects"
_PRINT_STATUS_PRINTING = "1"  # the "currently printing" filter value this endpoint expects

# Request-signing identity for the "Slicer Next" client. Anycubic issues one
# fixed app id/secret per client type (web app, Android app, slicer); these
# identify *which app* is calling, not a per-user secret, and are the same
# for every Slicer Next install.
_APP_ID = "f9b3528877c94d5c9c5af32245db46ef"
_APP_SECRET = "0cf75926606049a3937f56b0373b99fb"
_APP_VERSION = "V3.0.0"
_DEVICE_TYPE = "pcf"
_IS_CN = "1"

_TIMEOUT = 15
_USER_AGENT = f"{__app_name__}/{__version__}"


class CloudError(Exception):
    """Readable failure talking to the Anycubic cloud."""


def _sign_request() -> dict[str, str]:
    """Build the ``Xx-*`` signing headers the cloud API requires on every call.

    The signature just binds the app identity, a timestamp, and a random
    nonce together with MD5 so the server can tell the request came from a
    real client within a reasonable time window — it is not a secret
    exchange, and every Slicer Next install computes it the same way.
    """
    nonce = str(uuid.uuid1())
    timestamp = str(int(time.time() * 1000))
    digest_input = f"{_APP_ID}{timestamp}{_APP_VERSION}{_APP_SECRET}{nonce}{_APP_ID}"
    signature = hashlib.md5(digest_input.encode("utf-8")).hexdigest()
    return {
        "Xx-Device-Type": _DEVICE_TYPE,
        "Xx-Is-Cn": _IS_CN,
        "Xx-Nonce": nonce,
        "Xx-Signature": signature,
        "Xx-Timestamp": timestamp,
        "Xx-Version": _APP_VERSION,
        "Content-Type": "application/json",
        "XX-LANGUAGE": "US",
    }


# ------------------------------------------------------------------ token


def find_slicer_token() -> str:
    """Best-effort: read the access token Anycubic Slicer Next stored locally.

    ``AnycubicSlicerNext.conf`` turned out to encrypt its ``anycubic_cloud``
    section (both keys and values) at rest, so that document never has a
    plain ``access_token`` field to find — the original approach here silently
    found nothing. Slicer Next's own debug logs, however, print the JWT in
    clear text on every login (``AnycubicContext::login, accessToken = ...``),
    so this reads the most recent one from there instead, falling back to the
    (now largely theoretical) config-file lookup in case some build really
    does store it as plain JSON.

    Returns an empty string when nothing is found. Nothing is transmitted
    anywhere by this function; it only reads local files.
    """
    token = _find_token_in_logs()
    if token:
        return token
    return _find_token_in_config()


_TOKEN_LOG_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})[^\n]*?accessToken\s*=\s*(eyJ[\w\-.]+)"
)


def _find_token_in_logs() -> str:
    best_token = ""
    best_timestamp = ""
    for log_dir in _slicer_log_dir_candidates():
        try:
            log_files = list(log_dir.glob("*.log"))
        except OSError:
            continue
        for log_file in log_files:
            try:
                text = log_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for match in _TOKEN_LOG_RE.finditer(text):
                timestamp, token = match.group(1), match.group(2)
                if timestamp > best_timestamp:
                    best_timestamp = timestamp
                    best_token = token
    return best_token


def _find_token_in_config() -> str:
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


def _slicer_dir() -> Path | None:
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        return Path(appdata) / "AnycubicSlicerNext" if appdata else None
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "AnycubicSlicerNext"
    return Path.home() / ".config" / "AnycubicSlicerNext"


def _slicer_config_candidates() -> list[Path]:
    base = _slicer_dir()
    return [base / "AnycubicSlicerNext.conf"] if base else []


def _slicer_log_dir_candidates() -> list[Path]:
    base = _slicer_dir()
    return [base / "log"] if base else []


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

    id: str = ""  # stable identifier within the account, for re-matching later
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
        payload = json.dumps(
            {"device_type": _DEVICE_TYPE, "access_token": self._access_token}
        ).encode()
        response = self._request(_LOGIN_PATH, method="POST", body=payload, with_token=False)
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
        printers = [_parse_printer(entry) for entry in data if isinstance(entry, dict)]

        # The printer list itself carries no filename/progress; that lives on
        # the print job ("project"), fetched separately and merged in here.
        active = self._active_projects()
        for printer in printers:
            hit = active.get(printer.id)
            if hit is not None:
                printer.filename, printer.progress = hit
        return printers

    def _active_projects(self) -> dict[str, tuple[str, float]]:
        """Currently-printing jobs, keyed by printer id: ``(filename, progress)``.

        Best-effort: if this call fails, printer cards simply keep showing no
        filename/progress rather than the whole status fetch failing.
        """
        query = urllib.parse.urlencode(
            {"page": "1", "limit": "20", "print_status": _PRINT_STATUS_PRINTING}
        )
        try:
            response = self._request(f"{_PROJECTS_PATH}?{query}")
        except CloudError:
            return {}
        data = response.get("data")
        if not isinstance(data, list):
            return {}

        result: dict[str, tuple[str, float]] = {}
        for entry in data:
            if not isinstance(entry, dict):
                continue
            printer_id = str(entry.get("printer_id") or "")
            if not printer_id:
                continue

            filename = ""
            settings_raw = entry.get("settings")
            if isinstance(settings_raw, str):
                try:
                    filename = str(json.loads(settings_raw).get("filename") or "")
                except (json.JSONDecodeError, AttributeError):
                    filename = ""

            try:
                progress = float(entry.get("progress")) / 100.0
            except (TypeError, ValueError):
                progress = 0.0

            result[printer_id] = (filename, progress)
        return result

    # ------------------------------------------------------------- internal

    def _request(
        self, path: str, method: str = "GET", body: bytes | None = None, with_token: bool = True
    ) -> dict:
        headers = {**_sign_request(), "User-Agent": _USER_AGENT, "Origin": _AUTH_ORIGIN}
        if with_token and self._session_token:
            headers["XX-Token"] = self._session_token
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
    state = str(pick("print_status", "work_status", "state", "reason") or status_text)
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

    nested_params = entry.get("parameter")
    nested_params = nested_params if isinstance(nested_params, dict) else {}

    def temp(*keys: str) -> float:
        value = pick(*keys)
        if value is None:
            for key in keys:
                if nested_params.get(key) not in (None, ""):
                    value = nested_params[key]
                    break
        try:
            return round(float(value), 1)
        except (TypeError, ValueError):
            return 0.0

    printing_tokens = ("print", "busy", "working", "1")
    is_printing = any(token in state.lower() for token in printing_tokens) and (
        "finish" not in state.lower()
    )

    return CloudPrinter(
        id=str(pick("id", "device_id", "deviceId", "sn", "printer_id") or ""),
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
