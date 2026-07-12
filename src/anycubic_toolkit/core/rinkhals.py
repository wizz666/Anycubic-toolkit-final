"""Rinkhals integration.

`Rinkhals <https://github.com/rinkhals-community/Rinkhals>`_ is an open-source
custom firmware for Anycubic Kobra printers. It maintains two things this
toolkit can use directly, over stable public URLs (no scraping, no wizz.se):

* **A firmware catalog.** A root ``manifest.json`` maps each printer model code
  to a per-model firmware manifest. Each manifest lists stock Anycubic firmware
  versions with a changelog, MD5 and a real ``.swu`` download URL. This is how
  the Firmware Center shows available firmware and downloads for supported
  models.
* **Rinkhals releases.** Published on GitHub, queried through the Releases API,
  so the Rinkhals page can show the latest version and link to install guides.

Everything here is best-effort and cached: any network or parse failure yields
an empty result rather than raising, so the UI can fall back to official links.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from anycubic_toolkit import __app_name__, __version__
from anycubic_toolkit.core.config import cache_dir

RINKHALS_REPO = "rinkhals-community/Rinkhals"
RINKHALS_HOME = "https://github.com/rinkhals-community/Rinkhals"
RINKHALS_DOCS = "https://rinkhals-community.github.io/Rinkhals/"
_ROOT_MANIFEST_URL = (
    "https://raw.githubusercontent.com/rinkhals-community/Rinkhals/"
    "master/files/3-rinkhals/manifest.json"
)
_RELEASES_URL = f"https://api.github.com/repos/{RINKHALS_REPO}/releases/latest"

_TIMEOUT = 12
_USER_AGENT = f"{__app_name__}/{__version__}"


@dataclass
class FirmwareRelease:
    """One stock firmware version offered for a model."""

    version: str
    url: str
    changes: str = ""
    md5: str = ""
    date: str = ""  # ISO date (from a Unix timestamp), or ""


class RinkhalsClient:
    """Fetches the Rinkhals firmware catalog and release metadata (cached)."""

    def __init__(self, cache_root: Path | None = None) -> None:
        self._cache = (cache_root or cache_dir()) / "rinkhals"
        self._cache.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- firmware

    def firmware_for_model(self, model_code: str) -> list[FirmwareRelease]:
        """Return firmware versions for *model_code* (e.g. ``"K3"``).

        Empty when the model isn't in the Rinkhals catalog (e.g. very new
        models) or the catalog can't be reached.
        """
        code = (model_code or "").upper().strip()
        if not code:
            return []
        root = self._fetch_json(_ROOT_MANIFEST_URL, "root-manifest.json")
        if not isinstance(root, dict):
            return []
        repositories = root.get("firmware_repositories", {})
        manifest_url = repositories.get(code) if isinstance(repositories, dict) else None
        if not isinstance(manifest_url, str) or not manifest_url:
            return []

        manifest = self._fetch_json(manifest_url, f"manifest-{code.lower()}.json")
        if not isinstance(manifest, dict):
            return []

        releases: list[FirmwareRelease] = []
        for entry in manifest.get("firmwares", []):
            if not isinstance(entry, dict):
                continue
            version = entry.get("version")
            url = entry.get("url")
            if not isinstance(version, str) or not isinstance(url, str) or not url:
                continue
            supported = entry.get("supported_models")
            if isinstance(supported, list) and supported and code not in [
                str(s).upper() for s in supported
            ]:
                continue
            releases.append(
                FirmwareRelease(
                    version=version,
                    url=url,
                    changes=str(entry.get("changes") or ""),
                    md5=str(entry.get("md5") or ""),
                    date=_iso_date(entry.get("date")),
                )
            )
        releases.sort(key=lambda r: _version_key(r.version), reverse=True)
        return releases

    def is_supported_model(self, model_code: str) -> bool:
        """True when the Rinkhals catalog has a firmware repository for the model."""
        root = self._fetch_json(_ROOT_MANIFEST_URL, "root-manifest.json")
        if not isinstance(root, dict):
            return False
        repositories = root.get("firmware_repositories", {})
        return (
            isinstance(repositories, dict)
            and (model_code or "").upper().strip() in repositories
        )

    # -------------------------------------------------------------- release

    def latest_release(self) -> dict[str, str] | None:
        """Latest Rinkhals release: ``{"version", "url", "published"}`` or None."""
        data = self._fetch_json(_RELEASES_URL, "latest-release.json")
        if not isinstance(data, dict) or "tag_name" not in data:
            return None
        return {
            "version": str(data.get("tag_name", "")),
            "url": str(data.get("html_url", RINKHALS_HOME)),
            "published": _iso_date_from_str(data.get("published_at")),
        }

    # ------------------------------------------------------------- internal

    def _fetch_json(self, url: str, cache_name: str) -> object:
        cache_file = self._cache / cache_name
        text = _http_get(url)
        if text:
            try:
                cache_file.write_text(text, encoding="utf-8")
            except OSError:
                pass
        else:
            try:
                text = cache_file.read_text(encoding="utf-8")
            except OSError:
                return None
        try:
            return json.loads(_strip_json_comments(text))
        except json.JSONDecodeError:
            return None


# ------------------------------------------------------------------- helpers


def _http_get(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return ""


def _strip_json_comments(text: str) -> str:
    """Remove ``//`` line comments (the Rinkhals root manifest uses them)."""
    lines = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("//"):
            continue
        lines.append(line)
    return "\n".join(lines)


def _version_key(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in version.replace("-", ".").split("."):
        parts.append(int(chunk) if chunk.isdigit() else 0)
    return tuple(parts)


def _iso_date(value: object) -> str:
    if isinstance(value, (int, float)) and value > 0:
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return ""
    return ""


def _iso_date_from_str(value: object) -> str:
    if isinstance(value, str) and value:
        return value.split("T", 1)[0]
    return ""
