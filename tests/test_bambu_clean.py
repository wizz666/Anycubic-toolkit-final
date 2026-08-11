from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
import trimesh

from anycubic_toolkit.core import bambu_clean as bc
from anycubic_toolkit.core.bambu_clean import (
    BED_SIZE_MM,
    PRINTER_PROFILES,
    ThreeMFError,
    _fmt,
    _normalize_support_settings,
    clean_3mf,
    clean_3mf_all,
    process_batch,
    process_file,
    validate_geometry,
    validate_stl_passthrough,
)

from _bambu_fixtures import (
    MULTI_PLATE_MODEL_SETTINGS,
    bambu_style_3mf_bytes_extra_files,
    bambu_style_model_xml,
    make_fake_profiles_dir,
    multi_plate_model_xml,
    object_support_model_settings_xml,
    project_settings_with_global_support,
    split_part_external_model_xml,
    split_part_main_model_xml,
    two_object_model_xml,
    write_3mf,
)


def test_clean_strips_bambu_content_and_loads_in_trimesh(tmp_path: Path):
    src = tmp_path / "bambu_thing.3mf"
    write_3mf(src, bambu_style_model_xml(), bambu_style_3mf_bytes_extra_files())

    out = tmp_path / "bambu_thing_clean.3mf"
    result = clean_3mf(src, out)

    assert not result.used_fallback
    assert result.objects_found == 1
    assert result.inlined_components == 0

    with zipfile.ZipFile(out) as zf:
        assert set(zf.namelist()) == {
            "[Content_Types].xml",
            "_rels/.rels",
            "3D/3dmodel.model",
        }
        model_xml = zf.read("3D/3dmodel.model")

    # paint_color/mmu_segmentation are multi-color painting data and are kept
    # (Slicer Next reads them natively); Bambu's own slicer bookkeeping is not.
    assert b'paint_color="4"' in model_xml
    assert b'mmu_segmentation="4:0"' in model_xml
    for needle in (b"BambuStudio", b"Bambu Lab"):
        assert needle not in model_xml, f"{needle!r} should have been stripped"

    scene = trimesh.load(out, force="scene")
    mesh = list(scene.geometry.values())[0]
    assert len(mesh.faces) == 12
    assert mesh.is_watertight


def test_clean_inlines_production_extension_component(tmp_path: Path):
    src = tmp_path / "split_part.3mf"
    write_3mf(
        src,
        split_part_main_model_xml(),
        {"3D/Objects/object_2.model": split_part_external_model_xml()},
    )

    out = tmp_path / "split_part_clean.3mf"
    result = clean_3mf(src, out)

    assert result.inlined_components == 1
    with zipfile.ZipFile(out) as zf:
        assert "3D/Objects/object_2.model" not in zf.namelist()
        model_xml = zf.read("3D/3dmodel.model")
    # paint_color on the external part's triangles must survive inlining,
    # not just a same-file strip - this is the real-world path (Bambu Studio
    # splits geometry into a separate part file) that originally lost it.
    assert b'paint_color="4"' in model_xml
    assert b'mmu_segmentation="4:0"' in model_xml
    mesh = trimesh.load(out, force="mesh")
    assert len(mesh.faces) == 12
    assert mesh.is_watertight


def test_clean_keeps_object_support_settings(tmp_path: Path):
    src = tmp_path / "split_part.3mf"
    write_3mf(
        src,
        split_part_main_model_xml(),
        {
            "3D/Objects/object_2.model": split_part_external_model_xml(),
            "Metadata/model_settings.config": object_support_model_settings_xml("1"),
        },
    )

    out = tmp_path / "split_part_clean.3mf"
    result = clean_3mf(src, out)

    assert result.support_settings_kept
    with zipfile.ZipFile(out) as zf:
        assert "Metadata/model_settings.config" in zf.namelist()
        settings_xml = zf.read("Metadata/model_settings.config")

    assert b'<object id="1">' in settings_xml
    assert b'key="enable_support" value="1"' in settings_xml
    # support_type is normalized from "(manual)" to "(auto)" - "manual"
    # support relies on designer-placed support points this pipeline can't
    # read/carry over, so keeping it verbatim would produce a file with
    # support nominally enabled but nothing actually generated at slice time.
    assert b'key="support_type" value="normal(auto)"' in settings_xml
    assert b"normal(manual)" not in settings_xml
    # Non-support bookkeeping (matrix/name/extruder) is not carried over -
    # only the specific support-related keys are.
    for needle in (b"matrix", b'key="extruder"', b'key="name"'):
        assert needle not in settings_xml

    # The output still loads and slices fine with the extra metadata part.
    mesh = trimesh.load(out, force="mesh")
    assert len(mesh.faces) == 12
    assert mesh.is_watertight


def test_clean_falls_back_to_project_level_enable_support(tmp_path: Path):
    """Real-world bug: enable_support is sometimes only ever set at the
    project level (project_settings.config), never as a per-object override
    - an object whose own metadata has support_type/style but no
    enable_support key must still get enable_support from the project
    default, or Slicer Next opens the file with supports silently off."""
    src = tmp_path / "split_part.3mf"
    write_3mf(
        src,
        split_part_main_model_xml(),
        {
            "3D/Objects/object_2.model": split_part_external_model_xml(),
            "Metadata/project_settings.config": project_settings_with_global_support(),
        },
    )

    out = tmp_path / "split_part_clean.3mf"
    result = clean_3mf(src, out)

    assert result.support_settings_kept
    with zipfile.ZipFile(out) as zf:
        settings_xml = zf.read("Metadata/model_settings.config")

    assert b'<object id="1">' in settings_xml
    assert b'key="enable_support" value="1"' in settings_xml
    assert b'key="support_on_build_plate_only" value="1"' in settings_xml


def test_clean_object_level_support_wins_over_project_level(tmp_path: Path):
    """When both levels set the same key, the object's own override wins -
    matching Bambu Studio's own precedence."""
    src = tmp_path / "split_part.3mf"
    write_3mf(
        src,
        split_part_main_model_xml(),
        {
            "3D/Objects/object_2.model": split_part_external_model_xml(),
            "Metadata/model_settings.config": object_support_model_settings_xml("1"),
            "Metadata/project_settings.config": project_settings_with_global_support(
                {"support_type": "tree(auto)"}
            ),
        },
    )

    out = tmp_path / "split_part_clean.3mf"
    result = clean_3mf(src, out)

    assert result.support_settings_kept
    with zipfile.ZipFile(out) as zf:
        settings_xml = zf.read("Metadata/model_settings.config").decode("utf-8")

    # The object's own override (normal(manual) -> normalized to
    # normal(auto), from object_support_model_settings_xml) wins over the
    # project default (tree(auto)); the project-only key
    # (support_on_build_plate_only) still fills the gap since the object
    # doesn't set it.
    assert 'key="support_type" value="normal(auto)"' in settings_xml
    assert "tree(auto)" not in settings_xml
    assert 'key="support_on_build_plate_only" value="1"' in settings_xml


def test_clean_without_support_settings_omits_model_settings_file(tmp_path: Path):
    """A file with no per-object support overrides shouldn't get an empty/
    unnecessary Metadata/model_settings.config in the output."""
    src = tmp_path / "bambu_thing.3mf"
    write_3mf(src, bambu_style_model_xml(), bambu_style_3mf_bytes_extra_files())

    out = tmp_path / "bambu_thing_clean.3mf"
    result = clean_3mf(src, out)

    assert not result.support_settings_kept
    with zipfile.ZipFile(out) as zf:
        assert "Metadata/model_settings.config" not in zf.namelist()


def test_clean_raises_on_totally_unreadable_file(tmp_path: Path):
    src = tmp_path / "garbage.3mf"
    src.write_bytes(b"not a zip file")
    with pytest.raises(ThreeMFError):
        clean_3mf(src, tmp_path / "garbage_clean.3mf")


def test_clean_recenters_build_items_on_target_bed(tmp_path: Path):
    # two_object_model_xml positions both objects near the origin (not
    # centered on any bed) - a real Bambu file's items are positioned for
    # the *source* printer's bed, which can leave a part hanging off a
    # differently-sized/centered target bed even though it would easily fit
    # if centered (Slicer Next: "An object is laid over the boundary of the
    # plate"). Cleaning must recenter the whole group while preserving the
    # 40mm gap between the two objects (no reintroduced collision).
    src = tmp_path / "two_objects.3mf"
    write_3mf(src, two_object_model_xml())
    out = tmp_path / "two_objects_clean.3mf"
    result = clean_3mf(src, out, bed_size=(260.0, 260.0, 260.0))

    assert result.recentered

    scene = trimesh.load(out, force="scene")
    world_meshes = scene.dump(concatenate=False)
    assert len(world_meshes) == 2
    combined = trimesh.util.concatenate(world_meshes)
    bounds = combined.bounds
    center = (bounds[0] + bounds[1]) / 2.0
    assert center[0] == pytest.approx(130.0, abs=0.01)
    assert center[1] == pytest.approx(130.0, abs=0.01)

    x_centers = sorted((m.bounds[0][0] + m.bounds[1][0]) / 2.0 for m in world_meshes)
    assert x_centers[1] - x_centers[0] == pytest.approx(40.0, abs=0.01)


def test_clean_does_not_recenter_when_already_centered(tmp_path: Path):
    src = tmp_path / "part.3mf"
    write_3mf(src, bambu_style_model_xml())
    out = tmp_path / "part_clean.3mf"
    # A 20mm cube at the origin, target bed exactly 20x20 -> already
    # perfectly centered, nothing should move.
    result = clean_3mf(src, out, bed_size=(20.0, 20.0, 20.0))
    assert not result.recentered


def test_validate_scene_uses_world_space_positions(tmp_path: Path):
    src = tmp_path / "two_objects.3mf"
    write_3mf(src, two_object_model_xml())
    out = tmp_path / "two_objects_clean.3mf"
    clean_3mf(src, out)

    scene = trimesh.load(out, force="scene")
    result = validate_geometry(scene, src)

    assert result.num_objects == 2
    assert result.dims_mm[0] == pytest.approx(60.0, abs=0.01)


def test_validate_warns_when_oversized():
    mesh = trimesh.creation.box(extents=(300, 300, 300))
    result = validate_geometry(mesh, Path("big.stl"), bed_size=BED_SIZE_MM)
    assert result.status == "WARN"
    assert any("Scale by" in w for w in result.warnings)


def test_process_batch_reports_progress(tmp_path: Path):
    write_3mf(tmp_path / "a.3mf", bambu_style_model_xml())
    write_3mf(tmp_path / "b.3mf", bambu_style_model_xml())

    seen: list[tuple[int, str]] = []
    reports = process_batch(
        [tmp_path / "a.3mf", tmp_path / "b.3mf"], progress=lambda p, n: seen.append((p, n))
    )

    assert len(reports) == 2
    assert all(r.status == "OK" for r in reports)
    assert seen[0] == (0, "a.3mf")
    assert seen[-1] == (100, "")


def test_stl_passthrough_validates_and_copies(tmp_path: Path):
    mesh = trimesh.creation.box(extents=(20, 20, 20))
    src = tmp_path / "part.stl"
    mesh.export(src)

    out = tmp_path / "out" / "part.stl"
    result = validate_stl_passthrough(src, out)

    assert result.status == "OK"
    assert out.exists()


def test_process_file_writes_stl_when_requested(tmp_path: Path):
    write_3mf(tmp_path / "part.3mf", bambu_style_model_xml())
    reports = process_file(tmp_path / "part.3mf", write_stl=True)
    assert len(reports) == 1
    assert reports[0].output_stl is not None and reports[0].output_stl.exists()


# --------------------------------------------------------------------- plates


def test_clean_3mf_all_splits_multi_plate_file_into_separate_outputs(tmp_path: Path):
    # The two objects are 400mm apart (as Bambu Studio exports two objects
    # that actually belong to separate plates) - if treated as one group,
    # recentering couldn't make both fit a 260mm bed at once even though
    # each individually does. Metadata/model_settings.config's own <plate>
    # bookkeeping says they're unrelated jobs, so they must come out as two
    # separate files, each independently centered.
    src = tmp_path / "minicooler.3mf"
    write_3mf(
        src,
        multi_plate_model_xml(),
        {"Metadata/model_settings.config": MULTI_PLATE_MODEL_SETTINGS},
    )

    results = clean_3mf_all(src, tmp_path, "minicooler", bed_size=(260.0, 260.0, 260.0))

    assert len(results) == 2
    names = {r.output_path.name for r in results}
    assert names == {"minicooler_First_Part_clean.3mf", "minicooler_Second_Part_clean.3mf"}

    for result in results:
        assert result.recentered
        assert result.objects_found == 1
        mesh = trimesh.load(result.output_path, force="mesh")
        center = (mesh.bounds[0] + mesh.bounds[1]) / 2.0
        assert center[0] == pytest.approx(130.0, abs=0.01)
        assert center[1] == pytest.approx(130.0, abs=0.01)


def test_process_file_splits_multi_plate_file_into_multiple_reports(tmp_path: Path):
    src = tmp_path / "minicooler.3mf"
    write_3mf(
        src,
        multi_plate_model_xml(),
        {"Metadata/model_settings.config": MULTI_PLATE_MODEL_SETTINGS},
    )

    reports = process_file(src)

    assert len(reports) == 2
    assert all(r.status == "OK" for r in reports)
    assert all(r.input_path == src for r in reports)


def test_fmt_preserves_full_float_precision():
    # "%.6g" (6 significant figures) lost sub-micron precision on a real
    # ~200mm model with fine mechanical features (PRMGR_BC-6_V2_216x50.5's
    # StackLock mechanism): two vertices genuinely distinct by a fraction of
    # a micron rounded to the *same* 6-sig-fig text, and trimesh's default
    # vertex-merging on load then silently welded them together - turning a
    # watertight mesh into a non-manifold one (6 broken faces, edges shared
    # by 4 triangles instead of 2) on that real file. repr() must round-trip
    # exactly so this can never happen again.
    tricky = 128.00000005
    assert float(_fmt(tricky)) == tricky


def test_clean_reconstructs_project_settings_when_profiles_available(
    tmp_path: Path, monkeypatch
):
    """When Anycubic Slicer Next's own system profiles are found, and there
    are support overrides worth carrying, a full project-level
    Metadata/project_settings.config is reconstructed from them (machine +
    process + filament, real-world-verified to be what Slicer Next actually
    needs alongside model_settings.config for per-object overrides to take
    effect - shipping model_settings.config alone was silently ignored)."""
    profiles_root = make_fake_profiles_dir(tmp_path, printer="Kobra X")
    monkeypatch.setattr(
        bc.anycubic_profiles, "find_profiles_dir", lambda override=None: profiles_root
    )

    src = tmp_path / "split_part.3mf"
    write_3mf(
        src,
        split_part_main_model_xml(),
        {
            "3D/Objects/object_2.model": split_part_external_model_xml(),
            "Metadata/project_settings.config": project_settings_with_global_support(),
        },
    )
    out = tmp_path / "split_part_clean.3mf"
    result = clean_3mf(src, out, bed_size=PRINTER_PROFILES["Kobra X"])

    assert result.project_profile_reconstructed
    with zipfile.ZipFile(out) as zf:
        assert "Metadata/project_settings.config" in zf.namelist()
        project_settings = json.loads(zf.read("Metadata/project_settings.config"))

    assert project_settings["enable_support"] == "1"
    assert project_settings["support_on_build_plate_only"] == "1"
    assert project_settings["printer_settings_id"] == "Anycubic Kobra X 0.4 nozzle"
    assert "from" not in project_settings


def test_clean_skips_project_settings_when_profiles_unavailable(tmp_path: Path, monkeypatch):
    """Slicer Next not being installed (or not bundling this printer) must
    degrade gracefully - the per-object model_settings.config still gets
    written on its own, matching this feature's pre-existing behavior."""
    monkeypatch.setattr(bc.anycubic_profiles, "find_profiles_dir", lambda override=None: None)

    src = tmp_path / "split_part.3mf"
    write_3mf(
        src,
        split_part_main_model_xml(),
        {
            "3D/Objects/object_2.model": split_part_external_model_xml(),
            "Metadata/project_settings.config": project_settings_with_global_support(),
        },
    )
    out = tmp_path / "split_part_clean.3mf"
    result = clean_3mf(src, out)

    assert not result.project_profile_reconstructed
    assert result.support_settings_kept
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
    assert "Metadata/project_settings.config" not in names
    assert "Metadata/model_settings.config" in names


@pytest.mark.parametrize(
    ("input_type", "expected_type"),
    [
        ("tree(manual)", "tree(auto)"),
        ("normal(manual)", "normal(auto)"),
        ("tree(auto)", "tree(auto)"),
        ("normal(auto)", "normal(auto)"),
    ],
)
def test_normalize_support_settings_coerces_manual_to_auto(input_type, expected_type):
    """Real-world bug: a "(manual)" support_type carried over verbatim
    showed correctly in Slicer Next's Objects tab (Enable support on, Type
    Tree(manual)) but generated zero actual support material at slice time
    - "manual" needs designer-placed support points this pipeline has no
    way to read. Coercing to "(auto)" makes the file self-sufficient."""
    result = _normalize_support_settings({"support_type": input_type, "enable_support": "1"})
    assert result["support_type"] == expected_type
    assert result["enable_support"] == "1"


def test_normalize_support_settings_no_support_type_is_noop():
    settings = {"enable_support": "1", "support_style": "snug"}
    assert _normalize_support_settings(settings) == settings


def test_clean_declares_enough_filament_slots_for_painted_geometry(
    tmp_path: Path, monkeypatch
):
    """Real-world regression: a plate with paint_color/mmu_segmentation
    data (multi-color) also had support overrides, so it got a
    reconstructed project_settings.config - but that file declared only 1
    filament slot (the system profile default), and Slicer Next then
    treated the project as genuinely single-color and dropped the painted
    triangles once it started trusting the reconstructed file at all. The
    slot count must come from the plate's own retained geometry, not just
    default to 1."""
    profiles_root = make_fake_profiles_dir(tmp_path, printer="Kobra X")
    monkeypatch.setattr(
        bc.anycubic_profiles, "find_profiles_dir", lambda override=None: profiles_root
    )

    src = tmp_path / "split_part.3mf"
    write_3mf(
        src,
        split_part_main_model_xml(),
        {
            # split_part_external_model_xml() paints its first two
            # triangles (paint_color="4", mmu_segmentation="4:0") - two
            # distinct paint values, so 3 filament slots are needed
            # (2 painted + 1 unpainted base).
            "3D/Objects/object_2.model": split_part_external_model_xml(),
            "Metadata/project_settings.config": project_settings_with_global_support(),
        },
    )
    out = tmp_path / "split_part_clean.3mf"
    result = clean_3mf(src, out, bed_size=PRINTER_PROFILES["Kobra X"])

    assert result.project_profile_reconstructed
    with zipfile.ZipFile(out) as zf:
        project_settings = json.loads(zf.read("Metadata/project_settings.config"))
        model_xml = zf.read("3D/3dmodel.model")

    assert len(project_settings["filament_settings_id"]) == 3
    assert len(project_settings["filament_type"]) == 3
    # Paint data itself still made it through, same as before this fix.
    assert b'paint_color="4"' in model_xml
    assert b'mmu_segmentation="4:0"' in model_xml


def test_clean_plain_plate_declares_single_filament_slot(tmp_path: Path, monkeypatch):
    """A plate with no paint data at all shouldn't declare extra unused
    filament slots."""
    profiles_root = make_fake_profiles_dir(tmp_path, printer="Kobra X")
    monkeypatch.setattr(
        bc.anycubic_profiles, "find_profiles_dir", lambda override=None: profiles_root
    )

    src = tmp_path / "split_part.3mf"
    write_3mf(
        src,
        split_part_main_model_xml(),
        {
            "3D/Objects/object_2.model": split_part_external_model_xml(paint=False),
            "Metadata/project_settings.config": project_settings_with_global_support(),
        },
    )
    out = tmp_path / "split_part_clean.3mf"
    result = clean_3mf(src, out, bed_size=PRINTER_PROFILES["Kobra X"])

    assert result.project_profile_reconstructed
    with zipfile.ZipFile(out) as zf:
        project_settings = json.loads(zf.read("Metadata/project_settings.config"))

    assert len(project_settings["filament_settings_id"]) == 1
