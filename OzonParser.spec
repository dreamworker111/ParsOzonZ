# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for a single-file Windows build of Ozon Parser."""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files

ROOT = Path(SPECPATH).resolve()

datas = [(str(ROOT / "assets"), "assets")]
binaries = []
hiddenimports = [
    "playwright",
    "playwright.sync_api",
    "openpyxl",
    "browser_cookie3",
    "httpx",
    "orjson",
    "bs4",
    "lxml",
    "ozon_parser",
    "ozon_categories",
    "PyQt6",
    "PyQt6.QtCore",
    "PyQt6.QtGui",
    "PyQt6.QtWidgets",
    "PyQt6.sip",
]

# Fail fast if PyQt6 bindings are incomplete (Qt6 DLLs without QtCore.pyd).
try:
    import PyQt6.QtCore  # noqa: F401
    import PyQt6.QtWidgets  # noqa: F401
except ImportError as exc:
    raise SystemExit(
        "PyQt6 is broken or incomplete. Reinstall before building:\n"
        "  py -3 -m pip install --user --force-reinstall PyQt6 PyQt6-Qt6 PyQt6-sip"
    ) from exc

for package in ("PyQt6", "playwright"):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

datas += collect_data_files("ozon_parser")
datas += collect_data_files("ozon_categories")

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="OzonParser",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
