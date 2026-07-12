"""Application update checks.

The wizz.se ``/updates`` endpoint returns a manifest such as::

    {
        "version": "0.2.0",
        "channel": "stable",
        "url": "https://github.com/wizz666/anycubic-toolkit/releases/tag/v0.2.0",
        "notes": "Bug fixes and Firmware Center improvements."
    }

The updater only compares versions and hands the release URL to the UI —
installing updates is done through the browser / GitHub Releases, which keeps
the update path transparent and auditable for an open-source project.
"""

from __future__ import annotations

from dataclasses import dataclass

from anycubic_toolkit import __version__
from anycubic_toolkit.core.api import ApiError, WizzApiClient


@dataclass(frozen=True)
class UpdateInfo:
    """Result of an update check."""

    available: bool
    latest_version: str
    url: str
    notes: str


def parse_version(version: str) -> tuple[int, ...]:
    """Convert '1.2.10' into a comparable tuple; unknown parts become 0."""
    parts: list[int] = []
    for chunk in version.strip().lstrip("v").split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def check_for_updates(api: WizzApiClient, channel: str = "stable") -> UpdateInfo:
    """Query wizz.se and compare against the running version.

    Raises :class:`anycubic_toolkit.core.api.ApiError` when offline with no
    cached manifest.
    """
    manifest = api.get_updates(channel=channel)
    latest = str(manifest.get("version", "")).strip()
    if not latest:
        raise ApiError("Update manifest did not contain a version field.")
    return UpdateInfo(
        available=parse_version(latest) > parse_version(__version__),
        latest_version=latest,
        url=str(manifest.get("url", "")),
        notes=str(manifest.get("notes", "")),
    )
