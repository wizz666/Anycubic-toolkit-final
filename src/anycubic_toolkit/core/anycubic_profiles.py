"""Reconstruct a minimal, Anycubic-native ``project_settings.config`` from
Anycubic Slicer Next's own bundled system profiles (machine + process +
filament), instead of hand-patching a stripped Bambu one.

This mirrors the manual MakerWorld -> Kobra S1 conversion recipe (previously
done by hand, file by file) as reusable code: Slicer Next's own profiles are
always self-consistent with each other, so merging them sidesteps the "some
values have been replaced" migration dialog that hand-editing a foreign
(Bambu) profile can trigger. It also turned out to matter for a second
reason, found via real-world testing: Slicer Next silently ignores the
per-object overrides in a paired ``Metadata/model_settings.config`` when
there's no ``project_settings.config`` alongside it - shipping only the
per-object file was not enough on its own.

Everything here is best-effort: if Anycubic Slicer Next isn't installed on
this machine, or doesn't bundle a profile for the requested printer/nozzle,
callers fall back to skipping project-level reconstruction entirely (the
per-object ``model_settings.config`` still gets written on its own, same as
before this module existed).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Only 0.4mm is exposed anywhere in this app's own UI/pipeline today (the
# printer selector picks a body, not a nozzle) - every real file handled so
# far has also been a 0.4mm nozzle print, so this isn't a new restriction.
NOZZLE = "0.4"

# Layer heights with a "Standard" process profile bundled for every printer
# this app supports at the 0.4mm nozzle (confirmed against the real
# Anycubic Slicer Next install). "High Quality" variants exist for some
# printers/heights too but are deliberately not used here - one predictable
# tier is enough for "make supports work", not "match every original
# setting exactly".
_STANDARD_LAYER_HEIGHTS = ["0.08", "0.12", "0.16", "0.20", "0.24", "0.28"]

# Distinct placeholder colors assigned to each declared filament slot when
# filament_slots > 1. Anycubic's own system filament profiles don't define
# filament_colour at all (only an empty default_filament_colour), so every
# slot ended up with the same (blank) color and the same filament_settings_id
# string - Slicer Next then collapsed the visually-identical slots back down
# to one on load, even though multiple were declared. The actual color the
# user prints with comes from whatever real filament they pick in Slicer
# Next's own Filament panel afterward - these values only need to be
# distinct from each other, not meaningful.
_DEFAULT_SLOT_COLORS = [
    "#FFCC00", "#00B8D9", "#FF5630", "#36B37E",
    "#6554C0", "#FF8B00", "#0065FF", "#00C7E6",
]

# Keys that identify/version a *system* profile rather than describing the
# print itself - dropped during merge and replaced with fresh project-level
# identity fields, matching how Bambu/Orca's own project files are shaped.
_BOOKKEEPING_KEYS = {
    "from",
    "name",
    "type",
    "inherits",
    "setting_id",
    "version",
    "compatible_printers",
    "compatible_printers_condition",
    "filament_id",
    "instantiation",
    "is_custom_defined",
    "is_visible",
}

_CANDIDATE_PROFILE_DIRS = [
    Path(r"C:\Program Files\AnycubicSlicerNext\resources\profiles\Anycubic"),
    Path.home() / "AppData" / "Roaming" / "AnycubicSlicerNext" / "system" / "Anycubic",
]


@dataclass
class ProfileContext:
    """Cheap-to-compute ingredients for :func:`build_project_settings`,
    gathered once per source file (while its zip is still open) and reused
    per-plate - the one thing that varies per plate (*filament_slots*, how
    many paint_color-referenced extruders a specific plate's mesh needs)
    isn't known until after that plate's geometry has been inlined/filtered,
    so it's passed separately at call time rather than baked in here."""

    profiles_dir: Path
    printer_name: str
    filament_type: str
    layer_height: float


def find_profiles_dir(override: Optional[Path] = None) -> Optional[Path]:
    """Locate Anycubic Slicer Next's bundled system-profile directory.
    *override* (mainly for tests) is used as-is if given. Returns None if
    no candidate location exists - callers must treat that as "skip
    project-level reconstruction", not an error."""
    if override is not None:
        return override if override.is_dir() else None
    for candidate in _CANDIDATE_PROFILE_DIRS:
        if candidate.is_dir():
            return candidate
    return None


def _read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _strip_bookkeeping(data: dict) -> dict:
    return {k: v for k, v in data.items() if k not in _BOOKKEEPING_KEYS}


def _resolve_filament_chain(filament_dir: Path, name: str) -> Optional[dict]:
    """Walk a filament profile's ``inherits`` chain root-first, merging each
    layer over the last - matches Bambu/Orca's own profile-inheritance
    order (e.g. ``fdm_filament_common`` -> ``Anycubic PLA @acbase`` ->
    ``Anycubic PLA @Anycubic Kobra S1 0.4 nozzle``)."""
    chain: list[dict] = []
    seen: set[str] = set()
    current = name
    while current and current not in seen:
        seen.add(current)
        data = _read_json(filament_dir / f"{current}.json")
        if data is None:
            return None
        chain.append(data)
        current = data.get("inherits") or ""
    merged: dict = {}
    for layer in reversed(chain):
        merged.update(layer)
    return merged


def _closest_layer_height(available: list[str], target: float) -> str:
    return min(available, key=lambda h: abs(float(h) - target))


def build_project_settings(
    profiles_dir: Path,
    printer_name: str,
    filament_type: str,
    layer_height: float,
    overrides: dict[str, str],
    filament_slots: int = 1,
) -> Optional[dict]:
    """Merge Anycubic's own machine + process + filament system profiles
    for *printer_name* (a :data:`~anycubic_toolkit.core.bambu_clean.
    PRINTER_PROFILES` key, e.g. ``"Kobra S1"``) into one project-level
    settings dict - the same layering Bambu/Orca use when saving a real
    project 3MF - then applies *overrides* (settings the original designer
    actually customized) on top, highest priority.

    *filament_slots* controls how many filament/AMS-slot entries the
    project declares. A real multi-filament Bambu/Orca project stores one
    array entry per slot across ~170 process/filament keys (speeds,
    temperatures, filament_settings_id, ...) - a real file confirmed this
    exactly. Declaring only 1 slot for a plate whose mesh has per-triangle
    paint_color data referencing a second extruder made Slicer Next treat
    the project as genuinely single-filament and silently drop the painted
    color, once it started trusting this reconstructed project file at all
    (see :data:`~anycubic_toolkit.core.bambu_clean._OBJECT_SUPPORT_KEYS`'s
    docstring history). Machine/hardware-only keys (nozzle_diameter,
    extruder_offset, ...) are never broadcast - this printer has exactly
    one physical extruder regardless of how many filament slots a paint
    job references.

    Returns None if the matching profiles can't be found (printer/nozzle
    combo not bundled, or *profiles_dir* itself doesn't exist), so the
    caller can fall back to skipping project-level reconstruction."""
    machine_dir = profiles_dir / "machine"
    process_dir = profiles_dir / "process"
    filament_dir = profiles_dir / "filament"

    machine = _read_json(machine_dir / f"Anycubic {printer_name} {NOZZLE} nozzle.json")
    if machine is None:
        return None

    available_heights = [
        h
        for h in _STANDARD_LAYER_HEIGHTS
        if (
            process_dir / f"{h}mm Standard @Anycubic {printer_name} {NOZZLE} nozzle.json"
        ).exists()
    ]
    if not available_heights:
        return None
    height = _closest_layer_height(available_heights, layer_height)
    process = _read_json(
        process_dir / f"{height}mm Standard @Anycubic {printer_name} {NOZZLE} nozzle.json"
    )
    if process is None:
        return None

    filament_profile_name = f"Anycubic {filament_type} @Anycubic {printer_name} {NOZZLE} nozzle"
    filament = _resolve_filament_chain(filament_dir, filament_profile_name)
    if filament is None:
        filament_profile_name = f"Anycubic PLA @Anycubic {printer_name} {NOZZLE} nozzle"
        filament = _resolve_filament_chain(filament_dir, filament_profile_name)
    if filament is None:
        return None

    machine_stripped = _strip_bookkeeping(machine)
    process_stripped = _strip_bookkeeping(process)
    filament_stripped = _strip_bookkeeping(filament)
    # Any key set (or overwritten) by the process/filament layers is a
    # per-slot setting in a real project file; a key that only ever came
    # from the machine layer is hardware-only and stays single-valued.
    per_slot_keys = set(process_stripped) | set(filament_stripped)

    # Later layers win: hardware truths first, then print behavior, then
    # material specifics, then whatever the original designer customized.
    merged: dict = {}
    merged.update(machine_stripped)
    merged.update(process_stripped)
    merged.update(filament_stripped)
    merged.update(overrides)

    if filament_slots > 1:
        for key in per_slot_keys:
            value = merged.get(key)
            if isinstance(value, list) and len(value) == 1:
                merged[key] = value * filament_slots

        # Give each slot a genuinely distinct color, not just a repeated
        # blank one - see _DEFAULT_SLOT_COLORS for why this matters.
        colors = [_DEFAULT_SLOT_COLORS[i % len(_DEFAULT_SLOT_COLORS)] for i in range(filament_slots)]
        merged["filament_colour"] = colors
        merged["default_filament_colour"] = colors
        if "filament_multi_colour" in merged:
            merged["filament_multi_colour"] = colors

    merged["printer_settings_id"] = machine.get("name", f"Anycubic {printer_name} {NOZZLE} nozzle")
    merged["print_settings_id"] = process.get("name", f"{height}mm Standard")
    merged["filament_settings_id"] = [filament_profile_name] * filament_slots

    return merged
