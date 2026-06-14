# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir build of the Wactorz pywebview desktop shell.

Used by packaging/appimage/build-appimage.sh. The frozen binary is the parent
desktop shell; it re-execs itself with `--run-backend` to run the aiohttp
monitor backend (see wactorz/desktop/app.py), so the whole app is one exe.

This build bundles PySide6 / QtWebEngine (the AppImage flavour). The deb/rpm
flavour does NOT use PyInstaller — it installs a venv against the host's
WebKit2GTK (see packaging/linux/build-deb-rpm.sh).
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
    excludes=["tkinter"],
    noarchive=False,
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
