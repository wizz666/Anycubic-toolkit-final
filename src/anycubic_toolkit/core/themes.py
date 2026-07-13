"""Theming.

Two bundled themes (``dark`` and ``light``) are defined as QSS files in
``anycubic_toolkit/resources/themes``. Each theme also exposes a small color
palette used by widgets that paint programmatically (health bars, charts).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

RESOURCES_THEMES = Path(__file__).resolve().parent.parent / "resources" / "themes"


@dataclass(frozen=True)
class Palette:
    """Programmatic colors that complement the QSS stylesheet."""

    accent: str
    success: str
    warning: str
    danger: str
    text_muted: str
    card_bg: str


PALETTES: dict[str, Palette] = {
    "dark": Palette(
        accent="#26C6DA",
        success="#4CAF7D",
        warning="#E0A32E",
        danger="#E05252",
        text_muted="#8A93A5",
        card_bg="#1C2230",
    ),
    "light": Palette(
        accent="#0097A7",
        success="#2E8B57",
        warning="#B8860B",
        danger="#C0392B",
        text_muted="#5C6470",
        card_bg="#FFFFFF",
    ),
}


class ThemeManager(QObject):
    """Applies QSS themes to the running :class:`QApplication`."""

    theme_changed = Signal(str)

    def __init__(self, theme: str = "dark") -> None:
        super().__init__()
        self._theme = "dark"
        self.apply(theme)

    @property
    def theme(self) -> str:
        """Name of the active theme."""
        return self._theme

    @property
    def palette(self) -> Palette:
        """Programmatic palette for the active theme."""
        return PALETTES.get(self._theme, PALETTES["dark"])

    @staticmethod
    def available_themes() -> list[str]:
        """Names of all bundled themes."""
        if RESOURCES_THEMES.is_dir():
            return sorted(p.stem for p in RESOURCES_THEMES.glob("*.qss"))
        return ["dark", "light"]

    def apply(self, theme: str) -> None:
        """Load a theme's QSS and apply it application-wide."""
        path = RESOURCES_THEMES / f"{theme}.qss"
        if not path.exists():
            theme, path = "dark", RESOURCES_THEMES / "dark.qss"
        try:
            qss = path.read_text(encoding="utf-8")
        except OSError:
            qss = ""
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(qss)
        self._theme = theme
        self.theme_changed.emit(theme)
