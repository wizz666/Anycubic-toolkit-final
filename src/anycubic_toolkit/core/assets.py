"""Bundled asset locations (icons).

Resolves paths the same way as the i18n/theme resources, so they work both from
source and inside a PyInstaller bundle (where ``__file__`` lives under the
extracted ``anycubic_toolkit`` tree).
"""

from __future__ import annotations

from pathlib import Path

_RESOURCES = Path(__file__).resolve().parent.parent / "resources"
_ICONS = _RESOURCES / "icons"


def app_icon_path() -> str:
    """Best available application icon path (``.ico`` preferred on Windows)."""
    ico = _ICONS / "app.ico"
    if ico.exists():
        return str(ico)
    png = _ICONS / "app.png"
    return str(png) if png.exists() else ""
