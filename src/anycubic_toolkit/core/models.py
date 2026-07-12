"""Canonical Anycubic printer catalog.

A single source of truth for the printer models the toolkit knows about, shared
by log detection, the firmware catalog, the Rinkhals page and the manual model
selector. Model *codes* (``K3``, ``KS1``, ``K4P`` …) match the keys used by the
Rinkhals firmware manifest.
"""

from __future__ import annotations

# Ordered (code, display name) pairs — drives the manual model dropdown.
KNOWN_MODELS: list[tuple[str, str]] = [
    ("K2P", "Anycubic Kobra 2 Pro"),
    ("K3", "Anycubic Kobra 3"),
    ("K3V2", "Anycubic Kobra 3 V2"),
    ("K3M", "Anycubic Kobra 3 Max"),
    ("KS1", "Anycubic Kobra S1"),
    ("KS1M", "Anycubic Kobra S1 Max"),
    ("K4P", "Anycubic Kobra X"),
]

# Numeric modelId (older "gk" generation, from api.cfg) -> model code.
MODEL_ID_TO_CODE: dict[str, str] = {
    "20021": "K2P",
    "20024": "K3",
    "20025": "KS1",
    "20026": "K3M",
    "20027": "K3V2",
    "20029": "KS1M",
    "20030": "K4P",
}

_CODE_TO_NAME: dict[str, str] = dict(KNOWN_MODELS)


def model_name(code: str) -> str:
    """Display name for a model *code*, or an empty string if unknown."""
    return _CODE_TO_NAME.get((code or "").upper().strip(), "")


def model_name_or_code(code: str) -> str:
    """Display name for *code*, falling back to the code itself."""
    code = (code or "").upper().strip()
    return _CODE_TO_NAME.get(code, code)
