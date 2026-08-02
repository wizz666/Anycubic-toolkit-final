# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build specification for Anycubic Toolkit.

Produces a single windowed executable named ``AnycubicToolkit`` that bundles
the translation files, QSS themes and the built-in plugins.

Build locally with::

    pyinstaller anycubic_toolkit.spec
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

project_root = Path(SPECPATH)
package_root = project_root / "src" / "anycubic_toolkit"

datas = [
    (str(package_root / "resources" / "i18n"), "anycubic_toolkit/resources/i18n"),
    (str(package_root / "resources" / "themes"), "anycubic_toolkit/resources/themes"),
    (str(package_root / "resources" / "icons"), "anycubic_toolkit/resources/icons"),
    (str(project_root / "plugins"), "plugins"),
]

hiddenimports = collect_submodules("anycubic_toolkit") + collect_submodules("paho")

block_cipher = None

a = Analysis(
    [str(package_root / "__main__.py")],
    pathex=[str(project_root / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # scipy is explicitly excluded even though trimesh can optionally use it
    # (and it may be installed in a dev venv for unrelated ad-hoc scripting):
    # trimesh's connected-components code falls back to networkx (already a
    # hard dependency) when scipy isn't importable, and scipy alone is ~135MB
    # of the bundle - PyInstaller's static analysis would pull it in "just in
    # case" from trimesh's own try/except import if it's merely installed.
    excludes=["tkinter", "PySide6.QtWebEngineCore", "PySide6.Qt3DCore", "scipy"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="AnycubicToolkit",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(package_root / "resources" / "icons" / "app.ico")
    if (package_root / "resources" / "icons" / "app.ico").exists()
    else None,
)
