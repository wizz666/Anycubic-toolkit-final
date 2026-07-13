"""Internationalization.

Translations are plain JSON files in ``anycubic_toolkit/resources/i18n``.
Adding a new language is as simple as dropping ``<code>.json`` into that
folder — it is discovered automatically and appears in the language selector.

Keys use dot-notation (``"sidebar.dashboard"``). English is the fallback for
any key missing in the active language.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, Signal

RESOURCES_I18N = Path(__file__).resolve().parent.parent / "resources" / "i18n"

# Native display names for known languages; unknown codes fall back to the code.
LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "sv": "Svenska",
    "es": "Español",
    "pt": "Português",
    "de": "Deutsch",
    "fr": "Français",
    "it": "Italiano",
    "pl": "Polski",
}


class Translator(QObject):
    """Loads JSON language files and resolves dot-notation keys."""

    language_changed = Signal(str)

    def __init__(self, language: str = "en") -> None:
        super().__init__()
        self._fallback: dict[str, str] = self._load_file("en")
        self._strings: dict[str, str] = {}
        self._language = "en"
        self.set_language(language)

    # ------------------------------------------------------------------ API

    @property
    def language(self) -> str:
        """Currently active language code."""
        return self._language

    def available_languages(self) -> list[tuple[str, str]]:
        """Return ``(code, native_name)`` for every bundled language file."""
        langs: list[tuple[str, str]] = []
        if RESOURCES_I18N.is_dir():
            for file in sorted(RESOURCES_I18N.glob("*.json")):
                code = file.stem
                langs.append((code, LANGUAGE_NAMES.get(code, code)))
        return langs

    def set_language(self, code: str) -> None:
        """Switch the active language and notify listeners."""
        self._strings = self._load_file(code)
        self._language = code if self._strings or code == "en" else "en"
        self.language_changed.emit(self._language)

    def tr(self, key: str, **kwargs: object) -> str:
        """Translate *key*, formatting with *kwargs* if provided."""
        text = self._strings.get(key) or self._fallback.get(key) or key
        if kwargs:
            try:
                return text.format(**kwargs)
            except (KeyError, IndexError):
                return text
        return text

    # ------------------------------------------------------------- internal

    @staticmethod
    def _load_file(code: str) -> dict[str, str]:
        path = RESOURCES_I18N / f"{code}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return _flatten(data)
        except (OSError, json.JSONDecodeError):
            return {}


def _flatten(data: dict, prefix: str = "") -> dict[str, str]:
    """Flatten nested dicts into dot-notation keys."""
    flat: dict[str, str] = {}
    for key, value in data.items():
        full = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            flat.update(_flatten(value, full))
        else:
            flat[full] = str(value)
    return flat


# Convenience alias type for widgets that accept a translate function.
TrFunc = Callable[..., str]
