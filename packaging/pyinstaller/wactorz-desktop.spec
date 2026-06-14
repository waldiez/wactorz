# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir build of the Wactorz pywebview desktop shell.

Used by packaging/linux/build-appimage.sh. The frozen binary is the parent
desktop shell; it re-execs itself with `--run-backend` to run the aiohttp
monitor backend (see wactorz/desktop/app.py), so the whole app is one exe.

This build bundles PySide6 / QtWebEngine. The Flatpak build (see README) gets
Qt from its runtime instead and does not use PyInstaller.
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve().parents[1]  # noqa: F821  (SPECPATH injected by PyInstaller)

datas = [
    (str(ROOT / "static"), "static"),                              # SPA + docs site
    (str(ROOT / "wactorz" / "desktop" / "assets"), "wactorz/desktop/assets"),
]
datas += collect_data_files("webview")  # pywebview JS/template assets

# The backend is started via runpy.run_module("wactorz"), so its submodules are
# not statically discoverable from app.py — pull them in explicitly. Optional
# integrations with uninstalled heavy deps (torch, anthropic, …) are simply
# skipped by PyInstaller with a warning.
hiddenimports = collect_submodules("wactorz")

a = Analysis(
    [str(ROOT / "wactorz" / "desktop" / "app.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=[
        "gi",
        "tkinter",
        # Qt modules QtWebEngine doesn't need (each avoided hook = libs not collected)
        "PySide6.QtDataVisualization", "PySide6.QtCharts",
        "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
        "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput",
        "PySide6.Qt3DLogic", "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras",
        "PySide6.QtQuick3D", "PySide6.QtSensors", "PySide6.QtSerialPort",
        "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtSql",
        "PySide6.QtTest", "PySide6.QtHelp", "PySide6.QtDesigner",
        "PySide6.QtScxml", "PySide6.QtRemoteObjects", "PySide6.QtSpatialAudio",
        "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtUiTools",
    ],
    noarchive=False,
    optimize=2,
)
pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="wactorz-desktop",
    console=False,
    icon=str(ROOT / "wactorz" / "desktop" / "assets" / "icon.png"),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="wactorz-desktop",
)
