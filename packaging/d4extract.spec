# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the D4.Export desktop GUI.

Run via:

    pyinstaller packaging/d4extract.spec --noconfirm

or via the helper script ``packaging/build.ps1``.

This spec targets a Windows ``--onefile --windowed`` build. The three
landmines for this stack are:

1. **VTK** ships hundreds of dynamically-loaded ``vtkmodules.*`` submodules.
   PyInstaller's analyzer cannot statically discover them, so we pull
   them in with ``collect_submodules``. Without this you get
   ``ModuleNotFoundError: vtkmodules.vtkRenderingOpenGL2`` at runtime.

2. **PyVista** ships a ``_version.py`` plus theme and example data files
   it loads at import time. ``collect_data_files`` covers them.

3. **PySide6 + QtWebEngine** adds ~300 MB nobody uses here. We
   explicitly exclude QtWebEngine, QtPdf, QtMultimedia, and Qt3D modules
   to keep the .exe under ~600 MB.

If you hit a ``ModuleNotFoundError`` at runtime from a *new* module not
listed below, add it to ``hiddenimports`` and rebuild.
"""

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_submodules,
)


# ---------------------------------------------------------------------------
# Hidden imports
# ---------------------------------------------------------------------------

# VTK's submodules are loaded dynamically — must be enumerated explicitly.
hidden_vtk = collect_submodules("vtkmodules")

# PyVista pulls in matplotlib lazily for some plotting helpers; we keep
# its submodules explicit to avoid surprise import errors.
hidden_pyvista = collect_submodules("pyvista")
hidden_pyvistaqt = collect_submodules("pyvistaqt")

# Our own package — PyInstaller finds the entry point but misses
# modules loaded by string (e.g. dynamic widget loading). Be explicit.
hidden_d4extract = collect_submodules("d4extract")

hiddenimports = (
    hidden_vtk
    + hidden_pyvista
    + hidden_pyvistaqt
    + hidden_d4extract
    + [
        # pygltflib uses pkg_resources at runtime for version lookup.
        "pkg_resources",
        # Some Qt platform plugins are conditionally imported.
        "PySide6.QtSvg",
    ]
)


# ---------------------------------------------------------------------------
# Data files
# ---------------------------------------------------------------------------

datas = []
datas += collect_data_files("vtkmodules")
datas += collect_data_files("pyvista")
datas += collect_data_files("pyvistaqt")
# Bundle the curated JSON datasets we ship alongside the code.
datas += [("../data", "data")]


# ---------------------------------------------------------------------------
# Excluded modules — trim the bundle size
# ---------------------------------------------------------------------------

excludes = [
    # ~300 MB of Chromium we never load.
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    # Multimedia + 3D modules we do not use.
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DExtras",
    "PySide6.Qt3DLogic",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQml",
    # Test frameworks we never want to ship.
    "pytest",
    "tests",
]


# ---------------------------------------------------------------------------
# Analysis / build graph
# ---------------------------------------------------------------------------

block_cipher = None

a = Analysis(
    ["..\\src\\d4extract\\gui\\main.py"],
    pathex=["..\\src"],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
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
    name="d4extract",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,            # UPX-packing breaks Qt DLL signatures.
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,        # Windowed app — no console window.
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon="../assets/d4extract.ico",   # uncomment once you have an icon
)
