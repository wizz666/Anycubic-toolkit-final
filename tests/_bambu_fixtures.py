"""Shared helpers for building synthetic Bambu-style .3mf fixtures, used by
test_bambu_clean.py. Hand-built as raw XML/zip bytes (not via
core.bambu_clean's own XML-writing helpers) so the tests stay independent of
the implementation they're checking.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

CORE_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
PRODUCTION_NS = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"

CONTENT_TYPES = (
    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    b'<Default Extension="rels" ContentType='
    b'"application/vnd.openxmlformats-package.relationships+xml"/>'
    b'<Default Extension="model" ContentType='
    b'"application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
    b'<Default Extension="json" ContentType="application/json"/>'
    b'<Default Extension="png" ContentType="image/png"/>'
    b"</Types>\n"
)

RELS = (
    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    b'<Relationships xmlns='
    b'"http://schemas.openxmlformats.org/package/2006/relationships">'
    b'<Relationship Target="/3D/3dmodel.model" Id="rel0" Type='
    b'"http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
    b"</Relationships>\n"
)

# A plain 20mm cube: 8 vertices, 12 triangles (2 per face), consistent
# outward-facing winding so trimesh reports it watertight.
CUBE_VERTS = [
    (0, 0, 0), (20, 0, 0), (20, 20, 0), (0, 20, 0),
    (0, 0, 20), (20, 0, 20), (20, 20, 20), (0, 20, 20),
]
CUBE_TRIS = [
    (0, 2, 1), (0, 3, 2),
    (4, 5, 6), (4, 6, 7),
    (0, 1, 5), (0, 5, 4),
    (3, 7, 6), (3, 6, 2),
    (0, 4, 7), (0, 7, 3),
    (1, 2, 6), (1, 6, 5),
]


def _vertices_xml(verts) -> str:
    return "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in verts)


def _triangles_xml(tris, *, paint_first_two: bool = False) -> str:
    parts = []
    for i, (a, b, c) in enumerate(tris):
        if paint_first_two and i == 0:
            parts.append(f'<triangle v1="{a}" v2="{b}" v3="{c}" paint_color="4"/>')
        elif paint_first_two and i == 1:
            parts.append(f'<triangle v1="{a}" v2="{b}" v3="{c}" mmu_segmentation="4:0"/>')
        else:
            parts.append(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>')
    return "".join(parts)


def write_3mf(path: Path, model_xml: bytes, extra_files: dict[str, bytes] | None = None) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        zf.writestr("_rels/.rels", RELS)
        zf.writestr("3D/3dmodel.model", model_xml)
        for name, data in (extra_files or {}).items():
            zf.writestr(name, data)


def bambu_style_model_xml() -> bytes:
    """A single-object cube with paint/mmu attrs, Bambu metadata and a
    Bambu-specific namespace/requiredextensions — everything the cleaner is
    supposed to strip."""
    vertices_xml = _vertices_xml(CUBE_VERTS)
    triangles_xml = _triangles_xml(CUBE_TRIS, paint_first_two=True)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="{CORE_NS}" xmlns:BambuStudio="http://schemas.bambulab.com/package/2021"
       unit="millimeter" requiredextensions="BambuStudio">
<metadata name="Application">BambuStudio-01.09.00.75</metadata>
<metadata name="BambuStudio:3mfVersion">1</metadata>
<resources>
<object id="1" type="model" BambuStudio:extruder="2">
<mesh>
<vertices>{vertices_xml}</vertices>
<triangles>{triangles_xml}</triangles>
</mesh>
</object>
</resources>
<build>
<item objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0" BambuStudio:printable="1"/>
</build>
</model>""".encode("utf-8")


def project_settings_with_global_support(overrides: dict | None = None) -> bytes:
    """A Metadata/project_settings.config (JSON) with enable_support set
    only at the project level and listed as a system-default deviation -
    mirrors a real file (PRMGR_BC-6_V2_216x50.5) where enable_support was
    never a per-object override at all, only ever a project-wide one."""
    import json as _json

    data = {
        "nozzle_diameter": ["0.4"],
        "enable_support": "1",
        "support_on_build_plate_only": "1",
        "different_settings_to_system": ["enable_support;support_on_build_plate_only", "", ""],
    }
    if overrides:
        data.update(overrides)
    return _json.dumps(data).encode("utf-8")


def bambu_style_3mf_bytes_extra_files() -> dict[str, bytes]:
    return {
        "Metadata/plate_1.json": b'{"plate_id": 1, "printer_model": "Bambu Lab P1S"}',
        "Metadata/project_settings.config": b'{"nozzle_diameter": ["0.4"]}',
        "Auxiliaries/thumbnail.png": b"\x89PNG\r\n\x1a\nfakepngdata",
    }


def two_object_model_xml() -> bytes:
    """Two independent objects, each their own cube, each their own build
    item — used to check multi-object handling / --merge."""
    verts_xml = _vertices_xml(CUBE_VERTS)
    tris_xml = _triangles_xml(CUBE_TRIS)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="{CORE_NS}" unit="millimeter">
<resources>
<object id="1" type="model">
<mesh><vertices>{verts_xml}</vertices><triangles>{tris_xml}</triangles></mesh>
</object>
<object id="2" type="model">
<mesh><vertices>{verts_xml}</vertices><triangles>{tris_xml}</triangles></mesh>
</object>
</resources>
<build>
<item objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>
<item objectid="2" transform="1 0 0 0 1 0 0 0 1 40 0 0"/>
</build>
</model>""".encode("utf-8")


def multi_plate_model_xml() -> bytes:
    """Two objects positioned far apart, as Bambu Studio exports two
    objects that actually belong to two SEPARATE plates/print jobs (each
    plate's items are placed as if it alone owned the source bed) —
    combining them into one group would span far more than one target bed's
    worth of space even though each individually fits fine."""
    verts_xml = _vertices_xml(CUBE_VERTS)
    tris_xml = _triangles_xml(CUBE_TRIS)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="{CORE_NS}" unit="millimeter">
<resources>
<object id="1" type="model">
<mesh><vertices>{verts_xml}</vertices><triangles>{tris_xml}</triangles></mesh>
</object>
<object id="2" type="model">
<mesh><vertices>{verts_xml}</vertices><triangles>{tris_xml}</triangles></mesh>
</object>
</resources>
<build>
<item objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>
<item objectid="2" transform="1 0 0 0 1 0 0 0 1 400 0 0"/>
</build>
</model>""".encode("utf-8")


MULTI_PLATE_MODEL_SETTINGS = b"""<?xml version="1.0" encoding="UTF-8"?>
<config>
  <plate>
    <metadata key="plater_id" value="1"/>
    <metadata key="plater_name" value="First Part"/>
    <model_instance>
      <metadata key="object_id" value="1"/>
      <metadata key="instance_id" value="0"/>
    </model_instance>
  </plate>
  <plate>
    <metadata key="plater_id" value="2"/>
    <metadata key="plater_name" value="Second Part"/>
    <model_instance>
      <metadata key="object_id" value="2"/>
      <metadata key="instance_id" value="0"/>
    </model_instance>
  </plate>
</config>"""


def split_part_main_model_xml() -> bytes:
    """Object 1 has no local mesh — only a production-extension <components>
    pointing at an external part file, mirroring how BambuStudio splits an
    object's geometry into 3D/Objects/object_2.model."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="{CORE_NS}" xmlns:p="{PRODUCTION_NS}" unit="millimeter" requiredextensions="p">
<resources>
<object id="1" type="model" p:UUID="11111111-1111-1111-1111-111111111111">
<components>
<component p:path="/3D/Objects/object_2.model" objectid="2" p:UUID="22222222-2222-2222-2222-222222222222"/>
</components>
</object>
</resources>
<build>
<item objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>
</build>
</model>""".encode("utf-8")


def object_support_model_settings_xml(object_id: str = "1") -> bytes:
    """A Metadata/model_settings.config with a mix of designer-set
    support overrides (should be kept) and irrelevant per-object
    bookkeeping - matrix/name/extruder (should NOT be carried over)."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<config>
  <object id="{object_id}">
    <metadata key="name" value="thing.stl"/>
    <metadata key="enable_support" value="1"/>
    <metadata key="support_type" value="normal(manual)"/>
    <metadata key="extruder" value="1"/>
    <part id="1" subtype="normal_part">
      <metadata key="matrix" value="1 0 0 0 1 0 0 0 1 0 0 0"/>
    </part>
  </object>
</config>""".encode("utf-8")


def split_part_external_model_xml(*, paint: bool = True) -> bytes:
    verts_xml = _vertices_xml(CUBE_VERTS)
    tris_xml = _triangles_xml(CUBE_TRIS, paint_first_two=paint)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="{CORE_NS}" xmlns:p="{PRODUCTION_NS}" unit="millimeter">
<resources>
<object id="2" type="model" p:UUID="22222222-2222-2222-2222-222222222222">
<mesh><vertices>{verts_xml}</vertices><triangles>{tris_xml}</triangles></mesh>
</object>
</resources>
<build/>
</model>""".encode("utf-8")


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def make_fake_profiles_dir(tmp_path: Path, printer: str = "TestPrinter") -> Path:
    """A minimal synthetic Anycubic Slicer Next profile bundle - independent
    of whatever's actually installed on the machine running the tests, so
    these tests are reproducible on any machine (including CI, which has no
    Slicer Next install at all). Shared by test_anycubic_profiles.py (unit
    tests on the module directly) and test_bambu_clean.py (integration,
    monkeypatching find_profiles_dir to point here)."""
    root = tmp_path / "profiles"
    _write_json(
        root / "machine" / f"Anycubic {printer} 0.4 nozzle.json",
        {
            "type": "machine",
            "from": "system",
            "name": f"Anycubic {printer} 0.4 nozzle",
            "inherits": "",
            "printable_height": "999",
            "nozzle_diameter": ["0.4"],
            "bed_temperature": ["60"],
        },
    )
    _write_json(
        root / "process" / f"0.20mm Standard @Anycubic {printer} 0.4 nozzle.json",
        {
            "type": "process",
            "from": "system",
            "name": f"0.20mm Standard @Anycubic {printer} 0.4 nozzle",
            "inherits": "",
            "enable_support": "0",
            "support_type": "tree(auto)",
            "support_style": "default",
            "layer_height": "0.2",
        },
    )
    _write_json(
        root / "process" / f"0.28mm Standard @Anycubic {printer} 0.4 nozzle.json",
        {
            "type": "process",
            "from": "system",
            "name": f"0.28mm Standard @Anycubic {printer} 0.4 nozzle",
            "inherits": "",
            "enable_support": "0",
            "support_type": "tree(auto)",
            "support_style": "default",
            "layer_height": "0.28",
        },
    )
    _write_json(
        root / "filament" / "fdm_filament_common.json",
        {
            "type": "filament",
            "from": "system",
            "name": "fdm_filament_common",
            "inherits": "",
            "filament_density": ["1.24"],
            "filament_diameter": ["1.75"],
        },
    )
    _write_json(
        root / "filament" / f"Anycubic PLA @Anycubic {printer} 0.4 nozzle.json",
        {
            "type": "filament",
            "from": "system",
            "name": f"Anycubic PLA @Anycubic {printer} 0.4 nozzle",
            "inherits": "fdm_filament_common",
            "filament_type": ["PLA"],
            "nozzle_temperature": ["205"],
        },
    )
    return root
