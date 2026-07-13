"""Application configuration.

Settings are persisted as a single JSON document inside the platform's
canonical configuration directory:

* Windows: ``%APPDATA%/AnycubicToolkit/config.json``
* Linux:   ``~/.config/AnycubicToolkit/config.json``
* macOS:   ``~/Library/Application Support/AnycubicToolkit/config.json``

The manager is intentionally dependency-free (no QSettings) so that core
services can be unit-tested without a Qt application instance.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

_APP_DIR_NAME = "AnycubicToolkit"

DEFAULTS: dict[str, Any] = {
    "language": "en",
    "theme": "dark",
    "update_channel": "stable",
    "auto_update": True,
    "download_folder": str(Path.home() / "Downloads"),
    "printer_model_code": "",       # manually selected model (when no log)
    "moonraker_host": "",           # printer IP/hostname for live connection
    "moonraker_port": "7125",       # Moonraker API port (Rinkhals default)
    "cloud_enabled": False,         # opt-in Anycubic Cloud status (unofficial)
    "cloud_access_token": "",       # Slicer Next access token (stored locally)
    "notify_enabled": False,        # opt-in print-finished notifications
    "notify_provider": "ntfy",      # ntfy | discord | webhook
    "notify_target": "",            # topic name / webhook URL
    "notify_ntfy_server": "https://ntfy.sh",
    "ha_enabled": False,            # opt-in Home Assistant MQTT publishing
    "ha_host": "",                  # MQTT broker host (e.g. homeassistant.local)
    "ha_port": "1883",              # MQTT broker port
    "ha_username": "",
    "ha_password": "",
    "plugins_enabled": {},          # plugin_id -> bool
    "last_analysis": None,          # serialized LogAnalysisResult
    "splash_shown_donate_hint": False,
}


def config_dir() -> Path:
    """Return (and create) the per-user configuration directory."""
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    path = base / _APP_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def data_dir() -> Path:
    """Return (and create) the per-user data directory (caches, plugins)."""
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    path = base / _APP_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def user_plugins_dir() -> Path:
    """Directory where Marketplace-installed plugins live."""
    path = data_dir() / "plugins"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_dir() -> Path:
    """Directory for cached downloads (password DB, API responses)."""
    path = data_dir() / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


class ConfigManager:
    """Thread-safe JSON-backed key/value configuration store."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or (config_dir() / "config.json")
        self._lock = threading.RLock()
        self._data: dict[str, Any] = dict(DEFAULTS)
        self.load()

    @property
    def path(self) -> Path:
        """Location of the backing JSON file."""
        return self._path

    def load(self) -> None:
        """Load configuration from disk, merging over defaults."""
        with self._lock:
            self._data = dict(DEFAULTS)
            try:
                if self._path.exists():
                    stored = json.loads(self._path.read_text(encoding="utf-8"))
                    if isinstance(stored, dict):
                        self._data.update(stored)
            except (OSError, json.JSONDecodeError):
                # A corrupt config must never prevent the app from starting.
                pass

    def save(self) -> None:
        """Persist configuration atomically."""
        with self._lock:
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(self._path)

    def get(self, key: str, default: Any = None) -> Any:
        """Return a configuration value."""
        with self._lock:
            if default is None:
                default = DEFAULTS.get(key)
            return self._data.get(key, default)

    def set(self, key: str, value: Any, save: bool = True) -> None:
        """Set a configuration value and (by default) persist immediately."""
        with self._lock:
            self._data[key] = value
            if save:
                self.save()
