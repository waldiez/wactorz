# Packaging — desktop

Builds the `wactorz-desktop` pywebview app. The webview engine and tray are
chosen per platform (see `wactorz/desktop/app.py`):

| Platform | Webview | Tray |
|----------|---------|------|
| Linux | Qt / QtWebEngine (PySide6) | Qt `QSystemTrayIcon` |
| Windows | Edge **WebView2** (system runtime) | pystray (win32) |
| macOS | Cocoa / WKWebView | pystray (darwin) |

| Format | Platform | Strategy | Size |
|--------|----------|----------|------|
| **AppImage** | Linux | PyInstaller, bundles Qt | large (~300 MB) |
| **Flatpak** | Linux | runtime-provided Qt (TODO) | small |
| **Inno Setup** | Windows | PyInstaller + system WebView2 | small |

## Layout
```
packaging/
  pyinstaller/wactorz-desktop.spec                # onedir freeze (shared, platform-aware icon)
  linux/AppRun                                    # GNOME→xcb, disables QtWebEngine sandbox
  linux/io.waldiez.wactorz.desktop                # desktop entry
  linux/io.waldiez.wactorz.metainfo.template.xml  # AppStream metainfo (Flatpak)
  linux/build-appimage.sh                         # pyinstaller → AppDir → appimagetool
  linux/build-all.sh
  windows/wactorz.iss                             # Inno Setup installer
  windows/build-windows.ps1                       # pyinstaller → iscc → Setup.exe
```

## AppImage (Linux)
Prereqs: `pip install pyinstaller`, `appimagetool` on `PATH`. Build on the
**oldest glibc** you support (e.g. an Ubuntu 22.04 container). AppImages aren't
cross-built — build the aarch64 one on an aarch64 machine (arch auto-detected).
```sh
bash packaging/linux/build-appimage.sh    # → dist/Wactorz-<arch>.AppImage
# or: make package-appimage               # (rebuilds the frontend first)
```

## Windows (Inno Setup)
Uses the system **WebView2** runtime (preinstalled on Win10/11), so no Qt is
bundled — the installer stays small. Build **on Windows**:
```powershell
pip install pyinstaller
pip install -e ".[desktop]"               # pywebview + pythonnet (WebView2)
# Inno Setup installed, iscc.exe on PATH
packaging\windows\build-windows.ps1       # → dist\Wactorz-Setup-<ver>.exe
```
- WebView2 runtime: assumed present on Win10/11. To auto-install it for older
  hosts, drop `MicrosoftEdgeWebview2Setup.exe` next to `wactorz.iss` and
  uncomment the two marked lines (a registry check already gates it).
- The installer adds Start-Menu (+ optional desktop) shortcuts with the
  `io.waldiez.wactorz` AppUserModelID for proper taskbar grouping.

## Flatpak (Linux — TODO)
Planned: `io.waldiez.wactorz.yml` on `org.kde.Platform` (Qt6 + QtWebEngine from
the runtime), Python deps via `flatpak-pip-generator`, reusing the `.desktop` +
metainfo. Built with `flatpak-builder`, published to Flathub.

## macOS — TODO
Cocoa webview + pystray work today from source; a `.app`/`.dmg` build is not yet
scaffolded.
