from __future__ import annotations

from pathlib import Path

from anycubic_toolkit.core import anycubic_profiles as ap

from _bambu_fixtures import make_fake_profiles_dir


def test_find_profiles_dir_with_override(tmp_path: Path):
    root = make_fake_profiles_dir(tmp_path)
    assert ap.find_profiles_dir(override=root) == root
    assert ap.find_profiles_dir(override=tmp_path / "nonexistent") is None


def test_find_profiles_dir_no_candidates(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(ap, "_CANDIDATE_PROFILE_DIRS", [tmp_path / "does_not_exist"])
    assert ap.find_profiles_dir() is None


def test_build_project_settings_merges_layers_correctly(tmp_path: Path):
    root = make_fake_profiles_dir(tmp_path)
    result = ap.build_project_settings(
        root, "TestPrinter", "PLA", layer_height=0.2, overrides={"enable_support": "1"}
    )
    assert result is not None

    # Machine layer.
    assert result["printable_height"] == "999"
    # Filament layer, merged through the inherits chain (fdm_filament_common
    # -> Anycubic PLA @...).
    assert result["filament_density"] == ["1.24"]
    assert result["filament_type"] == ["PLA"]
    assert result["nozzle_temperature"] == ["205"]
    # Process layer picked by closest layer height (0.2 requested, matches
    # the 0.20mm profile exactly over the 0.28mm one).
    assert result["support_type"] == "tree(auto)"
    # The override (what the original designer actually customized) wins
    # over the process profile's own default (enable_support=0).
    assert result["enable_support"] == "1"

    # Bookkeeping keys from the source profiles are dropped, replaced with
    # fresh project-level identity fields.
    assert "from" not in result
    assert "inherits" not in result
    assert result["printer_settings_id"] == "Anycubic TestPrinter 0.4 nozzle"
    assert result["filament_settings_id"] == ["Anycubic PLA @Anycubic TestPrinter 0.4 nozzle"]


def test_build_project_settings_picks_closest_layer_height(tmp_path: Path):
    root = make_fake_profiles_dir(tmp_path)
    result = ap.build_project_settings(
        root, "TestPrinter", "PLA", layer_height=0.27, overrides={}
    )
    assert result is not None
    assert result["print_settings_id"] == "0.28mm Standard @Anycubic TestPrinter 0.4 nozzle"


def test_build_project_settings_falls_back_to_pla_when_filament_missing(tmp_path: Path):
    root = make_fake_profiles_dir(tmp_path)
    result = ap.build_project_settings(
        root, "TestPrinter", "PETG", layer_height=0.2, overrides={}
    )
    assert result is not None
    assert result["filament_settings_id"] == ["Anycubic PLA @Anycubic TestPrinter 0.4 nozzle"]


def test_build_project_settings_none_for_unknown_printer(tmp_path: Path):
    root = make_fake_profiles_dir(tmp_path)
    result = ap.build_project_settings(
        root, "NoSuchPrinter", "PLA", layer_height=0.2, overrides={}
    )
    assert result is None


def test_build_project_settings_default_single_filament_slot(tmp_path: Path):
    root = make_fake_profiles_dir(tmp_path)
    result = ap.build_project_settings(
        root, "TestPrinter", "PLA", layer_height=0.2, overrides={}
    )
    assert result is not None
    assert result["filament_settings_id"] == ["Anycubic PLA @Anycubic TestPrinter 0.4 nozzle"]
    assert result["filament_type"] == ["PLA"]


def test_build_project_settings_broadcasts_filament_and_process_keys_per_slot(tmp_path: Path):
    """Real-world bug: a plate whose mesh has paint_color data referencing
    a second extruder still got a project file declaring only 1 filament
    slot, so Slicer Next treated the project as genuinely single-color and
    silently dropped the painted color. filament_slots=N must broadcast
    every per-slot (process/filament-layer) key to N entries - but never
    machine-only hardware keys (this printer has exactly 1 physical
    extruder regardless of how many colors are painted onto the mesh)."""
    root = make_fake_profiles_dir(tmp_path)
    result = ap.build_project_settings(
        root, "TestPrinter", "PLA", layer_height=0.2, overrides={}, filament_slots=2
    )
    assert result is not None

    assert result["filament_settings_id"] == [
        "Anycubic PLA @Anycubic TestPrinter 0.4 nozzle",
        "Anycubic PLA @Anycubic TestPrinter 0.4 nozzle",
    ]
    # Filament-layer keys (merged through the inherits chain) broadcast to
    # both slots.
    assert result["filament_type"] == ["PLA", "PLA"]
    assert result["filament_density"] == ["1.24", "1.24"]
    assert result["nozzle_temperature"] == ["205", "205"]

    # Machine-only hardware keys stay single-valued - never broadcast.
    assert result["nozzle_diameter"] == ["0.4"]
    assert result["bed_temperature"] == ["60"]


def test_build_project_settings_gives_each_slot_a_distinct_color(tmp_path: Path):
    """Real-world bug: Anycubic's system filament profile defines no
    filament_colour at all, so every declared slot ended up with the same
    blank color and the same filament_settings_id string - Slicer Next then
    collapsed the visually-identical slots back down to one on load, even
    though 2 were declared. Each slot needs a genuinely distinct color."""
    root = make_fake_profiles_dir(tmp_path)
    result = ap.build_project_settings(
        root, "TestPrinter", "PLA", layer_height=0.2, overrides={}, filament_slots=3
    )
    assert result is not None
    colors = result["filament_colour"]
    assert len(colors) == 3
    assert len(set(colors)) == 3
    assert result["default_filament_colour"] == colors
