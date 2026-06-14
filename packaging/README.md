# Packaging — Linux desktop

Builds the `wactorz-desktop` pywebview app for Linux. One webview engine
everywhere: **Qt / QtWebEngine** (PySide6). No GTK path.

| Format | Strategy | Webview | Size | Notes |
|--------|----------|---------|------|-------|
| **AppImage** | PyInstaller frozen, self-contained | bundled Qt/QtWebEngine | large (~300 MB) | runs on any distro, no install |
| **Flatpak** | runtime-provided Qt, sandboxed | Qt/QtWebEngine from runtime | small | Flathub distribution + portal/desktop integration |

The tray follows the engine: Qt `QSystemTrayIcon` on Linux (both formats);
pystray's native backend is used only on macOS/Windows (see `wactorz/desktop/app.py`).

## Layout
```
packaging/
  pyinstaller/wactorz-desktop.spec                # onedir freeze (Qt) — AppImage
  linux/AppRun                                    # disables QtWebEngine sandbox, sets QT_API
  linux/io.waldiez.wactorz.desktop                # desktop entry (shared by both formats)
  linux/io.waldiez.wactorz.metainfo.template.xml  # AppStream metainfo (required by Flatpak)
  linux/build-appimage.sh                         # pyinstaller → AppDir → appimagetool
  linux/build-all.sh                              # convenience wrapper
```

## AppImage
Prereqs: `pip install pyinstaller`, plus `appimagetool` on `PATH` (or
`APPIMAGETOOL=/path/to/appimagetool`). Build on the **oldest glibc** you support
(e.g. an Ubuntu 22.04 container) so the AppImage runs broadly.
```sh
ARCH=x86_64 bash packaging/linux/build-appimage.sh   # → dist/Wactorz-x86_64.AppImage
```

## Flatpak (TODO — not yet scaffolded)
Planned: a `io.waldiez.wactorz.yml` manifest on `org.kde.Platform` (provides Qt6
+ QtWebEngine), Python deps generated via `flatpak-pip-generator`, reusing the
same `.desktop` + metainfo. Built with `flatpak-builder`, published to Flathub.

Or via Make (rebuilds the frontend first):
```sh
make package-appimage              # arch auto-detected from the host
```

## aarch64
AppImages are **not cross-built** — PyInstaller freezes for the host arch. To
get an aarch64 AppImage, run the **same** build on an aarch64 machine (the build
auto-detects the arch); you just need `pyinstaller` and an **aarch64**
`appimagetool` there:
```sh
make package-appimage              # on the arm64 laptop → dist/Wactorz-aarch64.AppImage
```

## Notes / TODO
- Build on the oldest glibc you support (e.g. an Ubuntu 22.04 container per arch)
  so the AppImage runs broadly.
