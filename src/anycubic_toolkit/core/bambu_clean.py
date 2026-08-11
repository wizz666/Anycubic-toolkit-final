"""Convert Bambu Lab / MakerWorld .3mf files into clean, printer-neutral
files for an Anycubic printer.

Vendored from the standalone ``bambu2kobra`` CLI tool (kept in its own repo
for people who just want a script) — same tested logic, reused here by the
Bambu Import page. The two copies are synced by hand when the logic changes,
the same pattern already used for this project's other shared-protocol code.

A 3MF file is a ZIP (OPC package). The pieces relevant here:

- ``_rels/.rels``       points at the main model part (usually
                        ``3D/3dmodel.model``, but not guaranteed by name).
- ``3D/3dmodel.model``  the actual geometry: ``<model><resources>
                        <object><mesh>...</mesh></object>...</resources>
                        <build><item objectid=".." transform=".."/>...
                        </build></model>``.
- Everything else (``Metadata/*.json``, ``Metadata/*.config``,
                        ``Auxiliaries/*``, thumbnails, ...) is slicer-specific
                        bookkeeping we don't want in a printer-neutral file.

Bambu/BambuStudio-authored files can also split an object's mesh into a
separate part file referenced via the 3MF *production extension*
(``<components><component p:path="/3D/Objects/object_2.model" .../>
</components>``). Anycubic Slicer Next / OrcaSlicer can't resolve that
external reference, so those components are inlined back into a plain
``<mesh>`` before anything else is stripped.
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import trimesh
from lxml import etree

from anycubic_toolkit.core import anycubic_profiles

# Known Anycubic printer build volumes (mm), keyed by display name. Sourced
# from store.anycubic.com per-model specs.
PRINTER_PROFILES: dict[str, tuple[float, float, float]] = {
    "Kobra X": (260.0, 260.0, 260.0),
    "Kobra S1": (250.0, 250.0, 250.0),
    "Kobra S1 Max": (350.0, 350.0, 350.0),
}


def _printer_name_for_bed_size(bed_size: tuple[float, float, float]) -> Optional[str]:
    for name, size in PRINTER_PROFILES.items():
        if size == bed_size:
            return name
    return None

# Default/legacy build volume (Kobra X) - kept as a plain constant since
# process_file/process_batch/clean_3mf all default to it.
BED_SIZE_MM: tuple[float, float, float] = PRINTER_PROFILES["Kobra X"]

CORE_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
_RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_MODEL_REL_TYPE = "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"

# Attributes we keep; everything else on these elements is slicer-only
# bookkeeping (pid/p1 material refs, production extension UUIDs/paths, ...)
# and gets dropped. paint_color/mmu_segmentation are deliberately kept: they
# encode per-triangle multi-color painting (e.g. Bambu Studio's "Color
# Painting" tool), which Anycubic Slicer Next reads natively since it's an
# OrcaSlicer fork sharing the same 3MF paint format - stripping them threw
# away a user's multi-color design for no compatibility reason.
_TRIANGLE_KEEP_ATTRS = {"v1", "v2", "v3", "paint_color", "mmu_segmentation"}
_OBJECT_KEEP_ATTRS = {"id", "type", "name"}
_VERTEX_KEEP_ATTRS = {"x", "y", "z"}
_ITEM_KEEP_ATTRS = {"objectid", "transform", "partnumber", "printable"}

# Per-object support settings worth carrying over from a designer's own
# Metadata/model_settings.config. A designer who deliberately marked a part
# as needing support (e.g. an overhanging lattice/frame) knows that better
# than a generic default - silently dropping it (as the rest of Bambu's
# per-object/project settings are, for printer-neutrality) just means
# Slicer Next opens the file with supports off and surfaces a confusing
# "floating regions" warning the designer had already solved.
_OBJECT_SUPPORT_KEYS = {
    "enable_support",
    "support_type",
    "support_style",
    "support_on_build_plate_only",
    "support_threshold_angle",
    "support_base_pattern",
    "support_interface_pattern",
    "support_top_z_distance",
    "support_bottom_z_distance",
    "tree_support_branch_diameter",
    "tree_support_branch_angle",
}


def _normalize_support_settings(settings: dict[str, str]) -> dict[str, str]:
    """Coerce a "(manual)" support_type (``tree(manual)``, ``normal
    (manual)``) to its "(auto)" equivalent.

    "(manual)" means "only where the designer manually placed support
    points in Bambu Studio's interactive support editor" - a separate
    proprietary point list this pipeline has no way to read or carry over
    (it is not per-triangle paint data). Carrying the value over as-is
    produces a file with support nominally enabled but zero actual support
    material generated at slice time. Confirmed against a real file: the
    reconstructed override showed correctly as Enable Support = on, Type =
    Tree(manual) in Slicer Next's Objects tab, yet the sliced preview had no
    Support/Support interface material at all - switching Type to
    Tree(auto) by hand was the only change needed to make ~2h58m/22g of
    tree supports actually appear. Auto-detecting overhangs with the same
    style/pattern/angle settings is a far better approximation of the
    designer's intent than silently generating nothing.
    """
    support_type = settings.get("support_type")
    if support_type and "(manual)" in support_type:
        settings = dict(settings)
        settings["support_type"] = support_type.replace("(manual)", "(auto)")
    return settings


_CONTENT_TYPES_XML = (
    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    b'<Default Extension="rels" ContentType='
    b'"application/vnd.openxmlformats-package.relationships+xml"/>'
    b'<Default Extension="model" ContentType='
    b'"application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
    b'<Default Extension="config" ContentType="application/xml"/>'
    b"</Types>\n"
)

_RELS_XML = (
    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    b'<Relationships xmlns='
    b'"http://schemas.openxmlformats.org/package/2006/relationships">'
    b'<Relationship Target="/3D/3dmodel.model" Id="rel0" Type='
    b'"http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
    b"</Relationships>\n"
)


class ThreeMFError(Exception):
    """Raised when a 3MF can't be parsed/cleaned structurally."""


@dataclass
class PlateInfo:
    """One Bambu Studio plate — an independently-printed job within a
    multi-plate project (e.g. a "Lid" and a "Handle" exported together in
    one .3mf but never meant to be printed on the same physical plate) —
    and the object ids that belong to it, per ``Metadata/model_settings.
    config``'s own ``<plate><model_instance>`` bookkeeping."""

    name: str
    object_ids: set[str]


@dataclass
class CleanResult:
    """What happened while cleaning one 3MF file."""

    output_path: Path
    used_fallback: bool = False
    objects_found: int = 0
    stripped_attrs_or_elements: int = 0
    dropped_files: list[str] = field(default_factory=list)
    inlined_components: int = 0
    recentered: bool = False
    support_settings_kept: bool = False
    project_profile_reconstructed: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass
class ValidationResult:
    """Geometry validation for one output file."""

    path: Path
    dims_mm: tuple[float, float, float] = (0.0, 0.0, 0.0)
    triangle_count: int = 0
    watertight: bool = False
    num_objects: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.errors:
            return "FAIL"
        if self.warnings:
            return "WARN"
        return "OK"


@dataclass
class ConversionReport:
    """Full result for one input file, as produced by :func:`process_file`."""

    input_path: Path
    output_3mf: Optional[Path] = None
    output_stl: Optional[Path] = None
    clean: Optional[CleanResult] = None
    validation: Optional[ValidationResult] = None
    errors: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.errors:
            return "FAIL"
        if self.validation is not None:
            return self.validation.status
        return "FAIL"


# --------------------------------------------------------------------- 3MF clean


def clean_3mf(
    input_path: Path,
    output_path: Path,
    bed_size: tuple[float, float, float] = BED_SIZE_MM,
) -> CleanResult:
    """Strip Bambu-specific content from *input_path*, writing a minimal,
    printer-neutral 3MF to *output_path*.

    Also recenters the build plate's items (as a group, preserving their
    relative layout) onto *bed_size* — the source file's items are usually
    positioned/centered for the original Bambu bed, which can leave a part
    hanging off the edge of a differently-sized or differently-centered
    target bed even though the part itself easily fits (Slicer Next then
    refuses to slice with "An object is laid over the boundary of the
    plate"). Falls back to a trimesh-based geometry re-export (no
    recentering) if the file can't be parsed structurally.
    """
    input_path = Path(input_path)
    output_path = Path(output_path)
    try:
        return _clean_3mf_structured(input_path, output_path, bed_size)
    except Exception as exc:  # noqa: BLE001 - deliberate catch-all fallback trigger
        return _clean_3mf_fallback(input_path, output_path, reason=str(exc))


def _clean_3mf_structured(
    input_path: Path, output_path: Path, bed_size: tuple[float, float, float]
) -> CleanResult:
    with zipfile.ZipFile(input_path) as zf:
        model_part = _find_main_model_part(zf)
        model_root = etree.fromstring(zf.read(model_part))
        if _local_name(model_root.tag) != "model":
            raise ThreeMFError("Main part is not a <model> root element")

        inlined = _inline_components(zf, model_root)
        object_support_settings = _read_object_support_settings(zf)
        global_support_settings = _read_global_support_settings(zf)
        project_profile_context = None
        if object_support_settings or global_support_settings:
            # Only worth preparing when there's an actual support override
            # to carry - otherwise this would add an unnecessary settings
            # file to every conversion, needed or not.
            project_profile_context = _prepare_project_profile_context(zf, bed_size)
        original_extra_files = [
            name
            for name in zf.namelist()
            if name not in (model_part, "[Content_Types].xml", "_rels/.rels")
            and not name.endswith("/")
        ]

    return _finish_clean(
        model_root,
        output_path,
        bed_size,
        inlined,
        original_extra_files,
        object_support_settings,
        global_support_settings,
        project_profile_context,
    )


def _finish_clean(
    model_root,
    output_path: Path,
    bed_size: tuple[float, float, float],
    inlined: int,
    original_extra_files: list[str],
    object_support_settings: dict[str, dict[str, str]] | None = None,
    global_support_settings: dict[str, str] | None = None,
    project_profile_context: "anycubic_profiles.ProfileContext | None" = None,
) -> CleanResult:
    """Strip/recenter an already-inlined (and, for multi-plate splits,
    already-filtered-to-one-plate) model tree and write it out."""
    stripped = _strip_extension_content(model_root)
    model_root.attrib.pop("requiredextensions", None)
    etree.cleanup_namespaces(model_root)

    recentered = _recenter_build_items(model_root, bed_size)

    top_objects = model_root.findall(f".//{{{CORE_NS}}}object")
    objects_found = len(top_objects)

    # Project-level support settings (e.g. enable_support is very often only
    # ever set there, never per-object) apply to every object on this plate
    # as a baseline; an object's own explicit override always wins.
    retained_ids = {obj.get("id") for obj in top_objects}
    kept_support_settings: dict[str, dict[str, str]] = {}
    for obj_id in retained_ids:
        merged = dict(global_support_settings or {})
        merged.update((object_support_settings or {}).get(obj_id, {}))
        if merged:
            kept_support_settings[obj_id] = merged

    project_settings = None
    if project_profile_context is not None and kept_support_settings:
        # This plate's own retained geometry decides how many filament
        # slots the reconstructed project needs to declare - a plate with
        # no paint data at all only needs 1, a painted one needs as many as
        # its distinct paint values reference (see
        # _plate_filament_slot_count).
        filament_slots = _plate_filament_slot_count(model_root)
        project_settings = anycubic_profiles.build_project_settings(
            project_profile_context.profiles_dir,
            project_profile_context.printer_name,
            project_profile_context.filament_type,
            project_profile_context.layer_height,
            global_support_settings or {},
            filament_slots=filament_slots,
        )

    model_xml = etree.tostring(
        model_root, xml_declaration=True, encoding="UTF-8", standalone=True
    )
    _write_minimal_3mf(output_path, model_xml, kept_support_settings, project_settings)

    return CleanResult(
        output_path=output_path,
        used_fallback=False,
        objects_found=objects_found,
        stripped_attrs_or_elements=stripped,
        dropped_files=original_extra_files,
        inlined_components=inlined,
        recentered=recentered,
        support_settings_kept=bool(kept_support_settings),
        project_profile_reconstructed=project_settings is not None,
    )


def clean_3mf_all(
    input_path: Path,
    output_dir: Path,
    stem: str,
    bed_size: tuple[float, float, float] = BED_SIZE_MM,
) -> list[CleanResult]:
    """Like :func:`clean_3mf`, but plate-aware.

    A Bambu Studio project can bundle several *plates* — independently
    printed jobs (e.g. "Cooler" / "Lid" / "Handle") — into one .3mf, each
    positioned as if it alone owned the source printer's bed. Combining all
    of them into a single group (what :func:`clean_3mf` does) can then span
    far more than one target bed's worth of space even though each plate
    individually fits fine, so Slicer Next refuses to slice with "An object
    is laid over the boundary of the plate". This splits such a file into
    one output per plate (named after the plate, e.g. ``{stem}_Handle_
    clean.3mf``), each recentered independently. A single-plate (or
    non-Bambu) file behaves exactly like one :func:`clean_3mf` call, just
    wrapped in a one-item list — so callers only need this one function.
    """
    input_path = Path(input_path)
    output_dir = Path(output_dir)
    try:
        return _clean_3mf_plates_structured(input_path, output_dir, stem, bed_size)
    except Exception as exc:  # noqa: BLE001 - deliberate catch-all fallback trigger
        out = output_dir / f"{stem}_clean.3mf"
        return [_clean_3mf_fallback(input_path, out, reason=str(exc))]


def _clean_3mf_plates_structured(
    input_path: Path,
    output_dir: Path,
    stem: str,
    bed_size: tuple[float, float, float],
) -> list[CleanResult]:
    with zipfile.ZipFile(input_path) as zf:
        model_part = _find_main_model_part(zf)
        base_root = etree.fromstring(zf.read(model_part))
        if _local_name(base_root.tag) != "model":
            raise ThreeMFError("Main part is not a <model> root element")

        original_extra_files = [
            name
            for name in zf.namelist()
            if name not in (model_part, "[Content_Types].xml", "_rels/.rels")
            and not name.endswith("/")
        ]
        object_support_settings = _read_object_support_settings(zf)
        global_support_settings = _read_global_support_settings(zf)
        project_profile_context = None
        if object_support_settings or global_support_settings:
            project_profile_context = _prepare_project_profile_context(zf, bed_size)
        plates = _detect_plates(zf)

        if not plates:
            inlined = _inline_components(zf, base_root)
            out = output_dir / f"{stem}_clean.3mf"
            return [
                _finish_clean(
                    base_root,
                    out,
                    bed_size,
                    inlined,
                    original_extra_files,
                    object_support_settings,
                    global_support_settings,
                    project_profile_context,
                )
            ]

        # Filter each plate's copy down to its own object(s) *before*
        # inlining, so each report's inlined-component count reflects only
        # that plate's own production-extension parts, not the whole file's.
        results: list[CleanResult] = []
        used_names: set[str] = set()
        for index, plate in enumerate(plates, start=1):
            plate_root = copy.deepcopy(base_root)
            _filter_to_plate(plate_root, plate.object_ids)
            inlined = _inline_components(zf, plate_root)
            suffix = _slugify(plate.name) or f"plate{index}"
            if suffix in used_names:
                suffix = f"{suffix}_{index}"
            used_names.add(suffix)
            out = output_dir / f"{stem}_{suffix}_clean.3mf"
            results.append(
                _finish_clean(
                    plate_root,
                    out,
                    bed_size,
                    inlined,
                    original_extra_files,
                    object_support_settings,
                    global_support_settings,
                    project_profile_context,
                )
            )
        return results


def _detect_plates(zf: zipfile.ZipFile) -> list[PlateInfo] | None:
    """Read Bambu Studio's own plate/object mapping from ``Metadata/
    model_settings.config``, if present. Returns None for a single-plate
    (or non-Bambu) file — combining everything into one group is exactly
    right in that case, nothing to split."""
    try:
        raw = zf.read("Metadata/model_settings.config")
    except KeyError:
        return None
    try:
        root = etree.fromstring(raw)
    except etree.XMLSyntaxError:
        return None

    plates: list[PlateInfo] = []
    for plate_el in root.findall("plate"):
        name = ""
        for md in plate_el.findall("metadata"):
            if md.get("key") == "plater_name":
                name = (md.get("value") or "").strip()
        object_ids: set[str] = set()
        for instance in plate_el.findall("model_instance"):
            for md in instance.findall("metadata"):
                if md.get("key") == "object_id" and md.get("value"):
                    object_ids.add(md.get("value"))
        if object_ids:
            plates.append(PlateInfo(name=name, object_ids=object_ids))

    return plates if len(plates) > 1 else None


def _read_object_support_settings(zf: zipfile.ZipFile) -> dict[str, dict[str, str]]:
    """Read Bambu Studio's own per-object overrides from ``Metadata/
    model_settings.config`` (if present) and return, keyed by object id,
    only the support-related ones (:data:`_OBJECT_SUPPORT_KEYS`). Everything
    else in that file (matrices, source offsets, mesh stats, ...) is
    slicer-internal bookkeeping already reconstructed by the geometry clean
    itself and isn't carried over."""
    try:
        raw = zf.read("Metadata/model_settings.config")
    except KeyError:
        return {}
    try:
        root = etree.fromstring(raw)
    except etree.XMLSyntaxError:
        return {}

    result: dict[str, dict[str, str]] = {}
    for obj_el in root.findall("object"):
        obj_id = obj_el.get("id")
        if obj_id is None:
            continue
        settings = {
            md.get("key"): md.get("value")
            for md in obj_el.findall("metadata")
            if md.get("key") in _OBJECT_SUPPORT_KEYS and md.get("value") is not None
        }
        if settings:
            result[obj_id] = _normalize_support_settings(settings)
    return result


def _read_global_support_settings(zf: zipfile.ZipFile) -> dict[str, str]:
    """Read Bambu Studio's project-level profile (``Metadata/
    project_settings.config``, a JSON file) and return whichever of
    :data:`_OBJECT_SUPPORT_KEYS` the designer explicitly customized away
    from their own system default, per that file's own
    ``different_settings_to_system`` bookkeeping (the same "only override
    what was actually customized" signal the manual MakerWorld->Kobra S1
    conversion workflow already relies on).

    ``enable_support`` in particular is very often only ever set at this
    project level, never as a per-object override - a real file confirmed
    this: an object's own metadata block had support_type/style/pattern
    overrides but no enable_support key at all, while project_settings.
    config had enable_support=1 listed as a system-default deviation. Object
    -level overrides always win over this project-level fallback (merged in
    by the caller)."""
    data = _read_source_project_settings(zf)
    if data is None:
        return {}

    customized: set[str] = set()
    for group in data.get("different_settings_to_system", []):
        if isinstance(group, str):
            customized.update(k for k in group.split(";") if k)

    settings = {
        key: str(data[key])
        for key in _OBJECT_SUPPORT_KEYS
        if key in customized and key in data
    }
    return _normalize_support_settings(settings)


def _read_source_project_settings(zf: zipfile.ZipFile) -> Optional[dict]:
    """Parse ``Metadata/project_settings.config`` (Bambu's project-level
    JSON profile) if present, or None."""
    try:
        raw = zf.read("Metadata/project_settings.config")
    except KeyError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _read_source_material_hints(zf: zipfile.ZipFile) -> tuple[str, float]:
    """Best-effort (filament_type, layer_height) from the source file's own
    project settings, with safe defaults when absent or malformed - used to
    pick which Anycubic system profile to reconstruct from, not treated as
    load-bearing."""
    data = _read_source_project_settings(zf) or {}
    filament_type = "PLA"
    raw_type = data.get("filament_type")
    if isinstance(raw_type, list) and raw_type and isinstance(raw_type[0], str):
        filament_type = raw_type[0]
    layer_height = 0.2
    try:
        layer_height = float(data.get("layer_height", 0.2))
    except (TypeError, ValueError):
        pass
    return filament_type, layer_height


def _prepare_project_profile_context(
    zf: zipfile.ZipFile, bed_size: tuple[float, float, float]
) -> Optional[anycubic_profiles.ProfileContext]:
    """Best-effort setup for Anycubic-native project_settings.config
    reconstruction (see :mod:`anycubic_toolkit.core.anycubic_profiles`) for
    the selected printer. Returns None (skip project-level reconstruction
    entirely) if Slicer Next's system profiles aren't found on this
    machine, or don't cover this printer - the per-object
    model_settings.config still gets written on its own either way.

    Deliberately doesn't build the final settings dict yet: how many
    filament slots to declare depends on a specific plate's own retained
    geometry (see :func:`_plate_filament_slot_count`), not known until
    after that plate has been inlined/filtered in :func:`_finish_clean`."""
    printer_name = _printer_name_for_bed_size(bed_size)
    if printer_name is None:
        return None
    profiles_dir = anycubic_profiles.find_profiles_dir()
    if profiles_dir is None:
        return None
    filament_type, layer_height = _read_source_material_hints(zf)
    return anycubic_profiles.ProfileContext(profiles_dir, printer_name, filament_type, layer_height)


def _plate_filament_slot_count(model_root) -> int:
    """How many filament/extruder slots this plate's paint data needs.

    A real multi-color Bambu project declares one array entry per filament
    slot across ~170 settings keys; declaring only 1 slot for a plate whose
    mesh has per-triangle paint_color/mmu_segmentation data referencing a
    second extruder made Slicer Next treat the reconstructed project as
    genuinely single-filament and silently drop the painted color, once it
    started trusting the file at all. Each distinct paint value found is
    assumed to reference a distinct extruder, plus one more for the
    unpainted base (extruder 1); returns 1 for a plate with no paint data."""
    distinct: set[str] = set()
    for tri in model_root.iter(f"{{{CORE_NS}}}triangle"):
        value = tri.get("paint_color") or tri.get("mmu_segmentation")
        if value:
            distinct.add(value)
    return len(distinct) + 1 if distinct else 1


def _filter_to_plate(model_root, object_ids: set[str]) -> None:
    """Prune every ``<build><item>`` and ``<resources><object>`` not in
    *object_ids*, leaving only one plate's own geometry."""
    build_el = model_root.find(f"{{{CORE_NS}}}build")
    if build_el is not None:
        for item in list(build_el.findall(f"{{{CORE_NS}}}item")):
            if item.get("objectid") not in object_ids:
                build_el.remove(item)
    resources_el = model_root.find(f"{{{CORE_NS}}}resources")
    if resources_el is not None:
        for obj in list(resources_el.findall(f"{{{CORE_NS}}}object")):
            if obj.get("id") not in object_ids:
                resources_el.remove(obj)


def _slugify(name: str) -> str:
    """Turn a Bambu plate name like 'Handle (Tree support)' into a safe,
    short filename fragment."""
    cleaned = re.sub(r"[^\w\- ]+", "", name).strip()
    cleaned = re.sub(r"\s+", "_", cleaned)
    return cleaned[:40]


def _clean_3mf_fallback(input_path: Path, output_path: Path, reason: str) -> CleanResult:
    try:
        scene = trimesh.load(input_path, force="scene")
    except Exception as exc:  # noqa: BLE001 - both parse attempts failed; report both reasons
        raise ThreeMFError(
            f"Could not clean {input_path.name}: structured parse failed "
            f"({reason}) and the trimesh fallback also failed to load it ({exc})."
        ) from exc
    if isinstance(scene, trimesh.Trimesh):
        scene = trimesh.Scene(scene)
    if len(scene.geometry) == 0:
        raise ThreeMFError(
            f"Could not clean {input_path.name}: structured parse failed "
            f"({reason}) and the trimesh fallback found no geometry either."
        )
    scene.export(output_path)
    return CleanResult(
        output_path=output_path,
        used_fallback=True,
        objects_found=len(scene.geometry),
        notes=[f"Structured XML clean failed ({reason}); used trimesh fallback export."],
    )


def _find_main_model_part(zf: zipfile.ZipFile) -> str:
    """Return the archive path of the main 3dmodel part, via ``_rels/.rels``."""
    names = zf.namelist()
    try:
        rels_xml = zf.read("_rels/.rels")
    except KeyError:
        if "3D/3dmodel.model" in names:
            return "3D/3dmodel.model"
        raise ThreeMFError("No _rels/.rels and no 3D/3dmodel.model in archive")

    root = etree.fromstring(rels_xml)
    for rel in root.findall(f"{{{_RELS_NS}}}Relationship"):
        rel_type = (rel.get("Type") or "").rstrip("/")
        if rel_type == _MODEL_REL_TYPE or rel_type.endswith("3dmodel"):
            target = (rel.get("Target") or "").lstrip("/")
            if target in names:
                return target
    if "3D/3dmodel.model" in names:
        return "3D/3dmodel.model"
    raise ThreeMFError("Could not locate the main 3dmodel part via _rels/.rels")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_transform(transform: str) -> np.ndarray:
    """Parse a 3MF ``transform`` attribute (12 numbers) into a 4x4 matrix
    such that ``world = M @ [x, y, z, 1]``.

    3MF transforms are given as ``m00 m01 m02 m10 m11 m12 m20 m21 m22 tx ty
    tz`` with ``p' = (m00*x+m10*y+m20*z+tx, m01*x+m11*y+m21*z+ty,
    m02*x+m12*y+m22*z+tz)``.
    """
    vals = [float(v) for v in transform.split()]
    if len(vals) != 12:
        raise ValueError(f"Malformed 3MF transform: {transform!r}")
    m00, m01, m02, m10, m11, m12, m20, m21, m22, tx, ty, tz = vals
    return np.array(
        [
            [m00, m10, m20, tx],
            [m01, m11, m21, ty],
            [m02, m12, m22, tz],
            [0.0, 0.0, 0.0, 1.0],
        ]
    )


def _read_mesh_arrays(
    mesh_el,
) -> tuple[np.ndarray, np.ndarray, list[dict[str, str]]]:
    """Returns (vertices, triangles, per-triangle extra attrs) - the extra
    dict for each triangle holds any kept attribute besides v1/v2/v3 (e.g.
    paint_color), so callers that reassemble triangles (inlining a
    production-extension component) don't silently drop it."""
    vertices_el = mesh_el.find(f"{{{CORE_NS}}}vertices")
    triangles_el = mesh_el.find(f"{{{CORE_NS}}}triangles")
    verts = [
        (float(v.get("x")), float(v.get("y")), float(v.get("z")))
        for v in (vertices_el if vertices_el is not None else [])
    ]
    tris = []
    tri_extra: list[dict[str, str]] = []
    for t in triangles_el if triangles_el is not None else []:
        tris.append((int(t.get("v1")), int(t.get("v2")), int(t.get("v3"))))
        tri_extra.append(
            {
                k: v
                for k, v in t.attrib.items()
                if k in _TRIANGLE_KEEP_ATTRS and k not in ("v1", "v2", "v3")
            }
        )
    return np.array(verts, dtype=float), np.array(tris, dtype=int), tri_extra


def _fmt(v: float) -> str:
    """Format a float for XML output losslessly (exact float64 round-trip).

    Originally used "%.6g" (6 significant figures), which is nowhere near
    enough for a real ~200mm model with fine mechanical features (snap-fits,
    lock mechanisms): two vertices that were genuinely distinct by a
    fraction of a micron could round to the *same* 6-sig-fig text, and
    trimesh's default vertex-merging on load then silently welds them
    together - stitching edges that were never meant to be joined and
    turning a perfectly watertight mesh into a non-manifold one (caught via
    a real file: PRMGR_BC-6_V2_216x50.5, 6 broken faces, edges shared by 4
    triangles instead of 2). ``repr()`` gives Python's shortest string that
    parses back to the *exact* original float64 - no precision loss, so no
    coordinates can ever collide that weren't already identical.
    """
    return repr(float(v))


def _make_mesh_element(
    verts: np.ndarray,
    tris: np.ndarray,
    tri_extra: list[dict[str, str]] | None = None,
):
    mesh_el = etree.Element(f"{{{CORE_NS}}}mesh")
    vertices_el = etree.SubElement(mesh_el, f"{{{CORE_NS}}}vertices")
    for x, y, z in verts:
        etree.SubElement(
            vertices_el, f"{{{CORE_NS}}}vertex", x=_fmt(x), y=_fmt(y), z=_fmt(z)
        )
    triangles_el = etree.SubElement(mesh_el, f"{{{CORE_NS}}}triangles")
    for i, (a, b, c) in enumerate(tris):
        extra = tri_extra[i] if tri_extra is not None else {}
        etree.SubElement(
            triangles_el,
            f"{{{CORE_NS}}}triangle",
            v1=str(int(a)),
            v2=str(int(b)),
            v3=str(int(c)),
            **extra,
        )
    return mesh_el


def _inline_components(zf: zipfile.ZipFile, model_root) -> int:
    """Resolve production-extension ``<components>`` that point at external
    part files, replacing them with a real inlined ``<mesh>``.

    Purely internal component references (assemblies of other ``<object>``
    elements within the same model part, no external path) are left alone —
    those are standard 3MF that slicers already handle fine; only the
    Bambu-style file-splitting is the actual compatibility problem.
    """
    inlined = 0
    part_cache: dict[str, "etree._Element"] = {}
    names = set(zf.namelist())

    def load_part(path: str):
        if path not in part_cache:
            part_cache[path] = etree.fromstring(zf.read(path))
        return part_cache[path]

    for obj in model_root.iter(f"{{{CORE_NS}}}object"):
        components_el = obj.find(f"{{{CORE_NS}}}components")
        if components_el is None:
            continue

        comps = components_el.findall(f"{{{CORE_NS}}}component")
        if not any(_component_path(c) for c in comps):
            continue  # internal-only assembly, nothing to inline

        all_verts: list[np.ndarray] = []
        all_tris: list[np.ndarray] = []
        all_tri_extra: list[dict[str, str]] = []
        vert_offset = 0
        for comp in comps:
            objectid = comp.get("objectid")
            transform = comp.get("transform")
            comp_path = _component_path(comp)

            if comp_path:
                archive_path = comp_path if comp_path in names else comp_path.lstrip("/")
                if archive_path not in names:
                    continue
                part_root = load_part(archive_path)
                target_obj = _find_object_by_id(part_root, objectid)
            else:
                target_obj = _find_object_by_id(model_root, objectid)

            if target_obj is None:
                continue
            mesh_el = target_obj.find(f"{{{CORE_NS}}}mesh")
            if mesh_el is None:
                continue

            verts, tris, tri_extra = _read_mesh_arrays(mesh_el)
            if len(verts) == 0:
                continue
            if transform:
                m = _parse_transform(transform)
                verts = (m[:3, :3] @ verts.T).T + m[:3, 3]
            all_verts.append(verts)
            all_tris.append(tris + vert_offset)
            all_tri_extra.extend(tri_extra)
            vert_offset += len(verts)

        if not all_verts:
            continue
        merged_verts = np.concatenate(all_verts, axis=0)
        merged_tris = np.concatenate(all_tris, axis=0)
        new_mesh_el = _make_mesh_element(merged_verts, merged_tris, all_tri_extra)
        obj.replace(components_el, new_mesh_el)
        inlined += 1

    return inlined


def _component_path(component_el) -> str | None:
    for attr_name, attr_val in component_el.attrib.items():
        if _local_name(attr_name) == "path":
            return attr_val
    return None


def _find_object_by_id(root, objectid: str | None):
    if objectid is None:
        # No id given: fall back to the first object in that part (the
        # common case for a single-object split-out part file).
        return root.find(f".//{{{CORE_NS}}}object")
    for obj in root.iter(f"{{{CORE_NS}}}object"):
        if obj.get("id") == objectid:
            return obj
    return root.find(f".//{{{CORE_NS}}}object")


def _is_unwanted_attr(attr_name: str, keep_local_names: set[str]) -> bool:
    """True if *attr_name* should be stripped: core 3MF attributes are never
    namespace-qualified, so any ``{uri}local`` attribute is by definition an
    extension attribute (Bambu paint/mmu, production p:UUID, etc.) — even if
    its local name happens to collide with a core attribute we'd otherwise
    keep (e.g. a vendor namespace's own "printable" attribute)."""
    if attr_name.startswith("{"):
        return True
    return attr_name not in keep_local_names


def _strip_extension_content(model_root) -> int:
    """Remove paint/mmu/material/production-extension attributes and elements
    from an already-inlined model tree. Returns a rough count of things
    removed (used for reporting, not load-bearing)."""
    stripped = 0

    for tri in model_root.iter(f"{{{CORE_NS}}}triangle"):
        for attr in list(tri.attrib):
            if _is_unwanted_attr(attr, _TRIANGLE_KEEP_ATTRS):
                del tri.attrib[attr]
                stripped += 1

    for vert in model_root.iter(f"{{{CORE_NS}}}vertex"):
        for attr in list(vert.attrib):
            if _is_unwanted_attr(attr, _VERTEX_KEEP_ATTRS):
                del vert.attrib[attr]
                stripped += 1

    for obj in model_root.iter(f"{{{CORE_NS}}}object"):
        for attr in list(obj.attrib):
            if _is_unwanted_attr(attr, _OBJECT_KEEP_ATTRS):
                del obj.attrib[attr]
                stripped += 1

    for item in model_root.iter(f"{{{CORE_NS}}}item"):
        for attr in list(item.attrib):
            if _is_unwanted_attr(attr, _ITEM_KEEP_ATTRS):
                del item.attrib[attr]
                stripped += 1

    # <metadata> (Application, CreationDate, BambuStudio:*, plate info, ...)
    for md in list(model_root.findall(f"{{{CORE_NS}}}metadata")):
        model_root.remove(md)
        stripped += 1

    # Resources we don't need for geometry-only printing: colorgroups,
    # basematerials, texture2d, etc. Keep <object> only.
    resources_el = model_root.find(f"{{{CORE_NS}}}resources")
    if resources_el is not None:
        for child in list(resources_el):
            if _local_name(child.tag) != "object":
                resources_el.remove(child)
                stripped += 1

    return stripped


def _local_bbox(mesh_el) -> tuple[np.ndarray, np.ndarray] | None:
    """Local-space (min, max) corners of a mesh's vertices, or None if empty."""
    verts, _, _ = _read_mesh_arrays(mesh_el)
    if len(verts) == 0:
        return None
    return verts.min(axis=0), verts.max(axis=0)


def _format_transform(m: np.ndarray) -> str:
    """Inverse of :func:`_parse_transform`: 4x4 ``world = M @ [x,y,z,1]``
    back to the 3MF's 12-number ``m00 m01 m02 m10 m11 m12 m20 m21 m22 tx ty
    tz`` layout."""
    vals = [
        m[0, 0], m[1, 0], m[2, 0],
        m[0, 1], m[1, 1], m[2, 1],
        m[0, 2], m[1, 2], m[2, 2],
        m[0, 3], m[1, 3], m[2, 3],
    ]
    return " ".join(_fmt(v) for v in vals)


def _recenter_build_items(model_root, bed_size: tuple[float, float, float]) -> bool:
    """Recenter all of a plate's build items together on *bed_size*'s X/Y
    center, preserving their relative layout (translation only - rotation,
    scale and Z are untouched).

    The source file's items are positioned for the original Bambu bed; if
    that bed's size or center convention differs from the target printer's,
    a part can end up hanging off the new plate's edge even though it would
    easily fit if centered - Slicer Next then refuses to slice ("An object
    is laid over the boundary of the plate"). Treating the whole plate as one
    group (rather than recentering each object independently) avoids
    reintroducing collisions between multiple objects on the same plate.
    Returns True if anything was actually moved.
    """
    build_el = model_root.find(f"{{{CORE_NS}}}build")
    if build_el is None:
        return False

    object_bbox: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for obj in model_root.iter(f"{{{CORE_NS}}}object"):
        mesh_el = obj.find(f"{{{CORE_NS}}}mesh")
        if mesh_el is None:
            continue
        bbox = _local_bbox(mesh_el)
        if bbox is not None and obj.get("id") is not None:
            object_bbox[obj.get("id")] = bbox

    movable: list[tuple["etree._Element", np.ndarray]] = []
    world_mins: list[np.ndarray] = []
    world_maxs: list[np.ndarray] = []
    for item in build_el.findall(f"{{{CORE_NS}}}item"):
        bbox = object_bbox.get(item.get("objectid"))
        if bbox is None:
            continue
        transform = item.get("transform")
        m = _parse_transform(transform) if transform else np.eye(4)
        local_min, local_max = bbox
        corners = np.array(
            [
                [x, y, z]
                for x in (local_min[0], local_max[0])
                for y in (local_min[1], local_max[1])
                for z in (local_min[2], local_max[2])
            ]
        )
        world_corners = (m[:3, :3] @ corners.T).T + m[:3, 3]
        world_mins.append(world_corners.min(axis=0))
        world_maxs.append(world_corners.max(axis=0))
        movable.append((item, m))

    if not movable:
        return False

    combined_min = np.min(world_mins, axis=0)
    combined_max = np.max(world_maxs, axis=0)
    group_center = (combined_min + combined_max) / 2.0

    delta = np.array(
        [bed_size[0] / 2.0 - group_center[0], bed_size[1] / 2.0 - group_center[1], 0.0]
    )
    if np.allclose(delta, 0.0, atol=1e-6):
        return False

    for item, m in movable:
        m = m.copy()
        m[0, 3] += delta[0]
        m[1, 3] += delta[1]
        item.set("transform", _format_transform(m))

    return True


def _make_model_settings_xml(object_support_settings: dict[str, dict[str, str]]) -> bytes:
    """Build a minimal ``Metadata/model_settings.config`` carrying only the
    per-object support overrides Slicer Next reads from that fixed path
    (Bambu/OrcaSlicer convention, not part of core 3MF - no relationship or
    declared content type is required for it, matching real Bambu Studio
    output, but we declare one anyway since we control both ends here)."""
    config_el = etree.Element("config")
    for obj_id, settings in object_support_settings.items():
        obj_el = etree.SubElement(config_el, "object", id=obj_id)
        for key, value in settings.items():
            etree.SubElement(obj_el, "metadata", key=key, value=value)
    return etree.tostring(config_el, xml_declaration=True, encoding="UTF-8", standalone=True)


def _write_minimal_3mf(
    output_path: Path,
    model_xml_bytes: bytes,
    object_support_settings: dict[str, dict[str, str]] | None = None,
    project_settings: dict | None = None,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES_XML)
        zf.writestr("_rels/.rels", _RELS_XML)
        zf.writestr("3D/3dmodel.model", model_xml_bytes)
        if object_support_settings:
            zf.writestr(
                "Metadata/model_settings.config",
                _make_model_settings_xml(object_support_settings),
            )
        if project_settings:
            zf.writestr(
                "Metadata/project_settings.config",
                json.dumps(project_settings, indent=1).encode("utf-8"),
            )


# ------------------------------------------------------------------ validation


def validate_geometry(
    geom, path: Path, bed_size: tuple[float, float, float] = BED_SIZE_MM
) -> ValidationResult:
    """Validate a trimesh ``Trimesh`` or ``Scene`` against the printer's bed."""
    result = ValidationResult(path=Path(path))
    try:
        if isinstance(geom, trimesh.Scene):
            # dump() moves each geometry into its world-space position via the
            # scene graph (the <item transform="..."> from the 3MF) - using
            # geom.geometry.values() directly would give each object's local
            # mesh coordinates, understating a multi-object plate's true size.
            world_geoms = [g for g in geom.dump(concatenate=False) if len(g.vertices) > 0]
            if not world_geoms:
                result.errors.append("No geometry found in file")
                return result
            result.num_objects = len(world_geoms)
            combined = (
                trimesh.util.concatenate(world_geoms) if len(world_geoms) > 1 else world_geoms[0]
            )
        else:
            combined = geom
            result.num_objects = _count_components(combined)

        result.triangle_count = int(len(combined.faces))
        result.watertight = bool(combined.is_watertight)

        bounds = combined.bounds
        if bounds is None or result.triangle_count == 0:
            result.errors.append("Mesh has no valid geometry (empty or degenerate)")
            return result

        dims = bounds[1] - bounds[0]
        result.dims_mm = tuple(round(float(d), 2) for d in dims)

        _check_bed_fit(result, dims, bed_size)
        _check_units(result, dims)

        if not result.watertight:
            result.warnings.append(
                "Mesh is not watertight (has holes) - may fail to slice cleanly"
            )
    except Exception as exc:  # noqa: BLE001 - surface as a validation failure
        result.errors.append(f"Validation failed: {exc}")
    return result


def _count_components(mesh: "trimesh.Trimesh") -> int:
    try:
        bodies = mesh.split(only_watertight=False)
        return max(1, len(bodies))
    except Exception:
        return 1


def _check_bed_fit(
    result: ValidationResult, dims: np.ndarray, bed_size: tuple[float, float, float]
) -> None:
    if all(d <= b + 1e-6 for d, b in zip(dims, bed_size)):
        return

    dims_text = f"{dims[0]:.1f}x{dims[1]:.1f}x{dims[2]:.1f}"
    bed_text = f"{bed_size[0]:.0f}x{bed_size[1]:.0f}x{bed_size[2]:.0f}"

    sorted_dims = sorted(dims, reverse=True)
    sorted_bed = sorted(bed_size, reverse=True)
    if all(d <= b + 1e-6 for d, b in zip(sorted_dims, sorted_bed)):
        result.warnings.append(
            f"Model ({dims_text} mm) exceeds the {bed_text} mm bed as currently "
            f"oriented, but fits if rotated to align its longest axis with the "
            f"bed's longest axis."
        )
        return

    scale = min(b / d for d, b in zip(dims, bed_size) if d > 0)
    result.warnings.append(
        f"Model ({dims_text} mm) exceeds the {bed_text} mm build volume. "
        f"Scale by ~{scale:.3f}x (~{scale * 100:.1f}%) to fit, or split into parts."
    )


def _check_units(result: ValidationResult, dims: np.ndarray) -> None:
    max_dim = float(max(dims))
    if max_dim < 1.0:
        result.warnings.append(
            f"Model is tiny (largest dimension {max_dim:.3f} mm) - possible unit "
            f"mismatch (e.g. authored in meters); consider scaling by 1000x."
        )
    elif max_dim > 1000.0:
        result.warnings.append(
            f"Model is huge (largest dimension {max_dim:.1f} mm) - possible unit "
            f"mismatch (e.g. authored in inches/cm treated as mm); check scale."
        )


# --------------------------------------------------------------------- pipeline


def process_file(
    input_path: Path,
    outdir: Optional[Path] = None,
    *,
    write_stl: bool = False,
    merge: bool = False,
    bed_size: tuple[float, float, float] = BED_SIZE_MM,
) -> list[ConversionReport]:
    """Clean (if 3MF) or pass through (if STL) one input file and validate
    the result(s). This is the single entry point any caller (CLI, GUI page)
    should use per file.

    Always returns a list: a normal (single-plate) input produces exactly
    one report, but a Bambu multi-plate project (see :func:`clean_3mf_all`)
    produces one report per plate — returning a list uniformly means callers
    don't need two code paths for "did this file split or not."
    """
    input_path = Path(input_path)
    out_dir = Path(outdir) if outdir else input_path.parent
    stem = input_path.stem
    suffix = input_path.suffix.lower()

    if suffix not in (".3mf", ".stl"):
        report = ConversionReport(input_path=input_path)
        report.errors.append(f"Unsupported file type: {suffix}")
        return [report]

    try:
        out_dir.mkdir(parents=True, exist_ok=True)

        if suffix == ".3mf":
            clean_results = clean_3mf_all(input_path, out_dir, stem, bed_size)
            return [
                _finish_report(
                    input_path,
                    clean_result,
                    write_stl=write_stl,
                    merge=merge,
                    bed_size=bed_size,
                )
                for clean_result in clean_results
            ]

        # .stl passthrough - no plates/geometry rewriting involved.
        report = ConversionReport(input_path=input_path)
        mesh_obj = trimesh.load(input_path, force="mesh")
        report.validation = validate_geometry(mesh_obj, input_path, bed_size)
        if write_stl:
            out_stl = out_dir / f"{stem}.stl"
            mesh_obj.export(out_stl)
            report.output_stl = out_stl
        return [report]

    except Exception as exc:  # noqa: BLE001 - one bad file shouldn't crash a batch
        report = ConversionReport(input_path=input_path)
        report.errors.append(str(exc))
        return [report]


def _finish_report(
    input_path: Path,
    clean_result: CleanResult,
    *,
    write_stl: bool,
    merge: bool,
    bed_size: tuple[float, float, float],
) -> ConversionReport:
    """Load one clean_3mf_all() output, validate it, and optionally export
    a companion .stl next to it."""
    report = ConversionReport(input_path=input_path)
    report.clean = clean_result
    out_3mf = clean_result.output_path
    report.output_3mf = out_3mf
    try:
        mesh_obj = trimesh.load(out_3mf, force="scene")
        # to_geometry() applies each object's <item transform="..."> (its
        # world-space position) before merging - just concatenating
        # geom.geometry.values() would use each object's local mesh
        # coordinates and silently pile every part on top of each other.
        if merge and isinstance(mesh_obj, trimesh.Scene):
            mesh_obj = mesh_obj.to_geometry()

        report.validation = validate_geometry(mesh_obj, input_path, bed_size)

        if write_stl:
            export_obj = mesh_obj
            if isinstance(export_obj, trimesh.Scene):
                export_obj = export_obj.to_geometry()
            out_stl = out_3mf.with_name(out_3mf.stem.removesuffix("_clean") + ".stl")
            export_obj.export(out_stl)
            report.output_stl = out_stl
    except Exception as exc:  # noqa: BLE001 - one bad plate shouldn't crash a batch
        report.errors.append(str(exc))
    return report


def process_batch(
    input_paths: list[Path],
    outdir: Optional[Path] = None,
    *,
    write_stl: bool = False,
    merge: bool = False,
    bed_size: tuple[float, float, float] = BED_SIZE_MM,
    progress: Optional[Callable[[int, str], None]] = None,
) -> list[ConversionReport]:
    """Process several files, reporting ``(percent, filename)`` if *progress*
    is given — matches the convention :class:`~anycubic_toolkit.core.workers.
    FunctionWorker` looks for, so this can be handed to it directly.

    A Bambu multi-plate input can expand into several reports per file (see
    :func:`process_file`); the returned list is flattened accordingly.
    """
    reports: list[ConversionReport] = []
    total = len(input_paths) or 1
    for i, path in enumerate(input_paths):
        if progress:
            progress(int(i / total * 100), path.name)
        reports.extend(
            process_file(path, outdir, write_stl=write_stl, merge=merge, bed_size=bed_size)
        )
    if progress:
        progress(100, "")
    return reports


def validate_stl_passthrough(
    input_path: Path,
    output_path: Optional[Path] = None,
    bed_size: tuple[float, float, float] = BED_SIZE_MM,
) -> ValidationResult:
    """Validate an .stl in place, optionally copying it to *output_path*."""
    input_path = Path(input_path)
    mesh = trimesh.load(input_path, force="mesh")
    result = validate_geometry(mesh, input_path, bed_size)
    if output_path is not None and Path(output_path) != input_path:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(input_path, output_path)
    return result
