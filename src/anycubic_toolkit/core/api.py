"""wizz.se REST API client.

The Toolkit talks to a WordPress plugin on https://wizz.se that exposes a
namespaced REST API. All calls are plain HTTPS GETs returning JSON. The
client is synchronous by design — UI code wraps calls in worker threads
(see :mod:`anycubic_toolkit.core.workers`).

Endpoints
---------
``/keys``               password database for AC_LOG.pack archives
``/news``               dashboard news feed
``/updates``            application update manifest

wizz.se hosts only the log-password database and the app's own news/update
channel. Error codes and firmware are fetched directly from Anycubic (see
:mod:`anycubic_toolkit.core.websources`), not mirrored here.

Every endpoint has an offline fallback: responses are cached on disk and the
cache is served whenever the network is unavailable.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from anycubic_toolkit import __api_base__, __version__
from anycubic_toolkit.core.config import cache_dir

USER_AGENT = f"AnycubicToolkit/{__version__}"
DEFAULT_TIMEOUT = 15.0
CACHE_MAX_AGE = 6 * 60 * 60  # seconds


class ApiError(RuntimeError):
    """Raised when the wizz.se API cannot be reached and no cache exists."""


class WizzApiClient:
    """Minimal, dependency-free JSON client with disk caching."""

    def __init__(self, base_url: str = __api_base__, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._cache_dir = cache_dir() / "api"
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ endpoints

    def get_key_database(self) -> dict[str, Any]:
        """Full password-database document used to unlock ``AC_LOG.pack``.

        The wizz.se document has the shape::

            {"schema": 1, "updated": "<iso>",
             "passwords": ["..."],
             "models": [{"model": "...", "log_password": "..."}, ...]}

        Returns an empty dict if the endpoint is unavailable and uncached.
        """
        data = self._get_json("/keys")
        return data if isinstance(data, dict) else {"passwords": data or []}

    def get_keys(self) -> list[dict[str, Any]]:
        """Per-model password entries (``model`` / ``log_password``).

        Kept for convenience; prefer :meth:`get_key_database` for the full
        document including the top-level ``passwords`` list and ``updated``.
        """
        data = self._get_json("/keys")
        if isinstance(data, list):
            return data
        models = data.get("models")
        if isinstance(models, list):
            return models
        return data.get("keys", [])

    def get_news(self) -> list[dict[str, Any]]:
        """News items for the dashboard."""
        data = self._get_json("/news")
        return data if isinstance(data, list) else data.get("news", [])

    def get_updates(self, channel: str = "stable") -> dict[str, Any]:
        """Application update manifest for the given channel."""
        data = self._get_json(f"/updates?channel={urllib.parse.quote(channel)}")
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------ transport

    def download_file(self, url: str, destination: Path) -> Path:
        """Download an arbitrary file (firmware, plugin archive) to *destination*."""
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        destination.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            destination.write_bytes(response.read())
        return destination

    def _get_json(self, endpoint: str) -> Any:
        """GET *endpoint* as JSON, falling back to the on-disk cache."""
        url = self.base_url + endpoint
        cache_file = self._cache_file(endpoint)
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
            )
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
            data = json.loads(raw)
            self._write_cache(cache_file, data)
            return data
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            cached = self._read_cache(cache_file)
            if cached is not None:
                return cached
            raise ApiError(f"wizz.se API unreachable: {url} ({exc})") from exc

    # ---------------------------------------------------------------- cache

    def _cache_file(self, endpoint: str) -> Path:
        safe = "".join(c if c.isalnum() else "_" for c in endpoint.strip("/"))
        return self._cache_dir / f"{safe or 'root'}.json"

    @staticmethod
    def _write_cache(path: Path, data: Any) -> None:
        try:
            path.write_text(
                json.dumps({"ts": time.time(), "data": data}), encoding="utf-8"
            )
        except OSError:
            pass

    @staticmethod
    def _read_cache(path: Path, max_age: float | None = None) -> Any | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if max_age is not None and time.time() - payload.get("ts", 0) > max_age:
                return None
            return payload.get("data")
        except (OSError, json.JSONDecodeError):
            return None
