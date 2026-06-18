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
  macos/entitlements.plist                        # hardened-runtime entitlements
  macos/build-macos.sh                            # pyinstaller → codesign → dmg → notarize
```

## AppImage (Linux)
Prereqs: `pip install pyinstaller`, `appimagetool` on `PATH`. Build on the
**oldest glibc** you support (e.g. an Ubuntu 22.04 container). AppImages aren't
cross-built — build the aarch64 one on an aarch64 machine (arch auto-detected).
```sh
bash packaging/linux/build-appimage.sh    # → dist/Wactorz-<arch>.AppImage
# or: make package-appimage               # (rebuilds the frontend first)
```

### Portable build via Docker (recommended for releases)
A native build inherits the build host's glibc as its floor and only bundles
libraries present on that host — so it can crash on leaner machines (e.g. the
QtWebEngine `libwebp.so.6` dependency missing). Build in the Ubuntu 22.04
container instead (`Dockerfile.appimage`, glibc 2.35): it pins a low glibc,
installs the Qt/X11/WebEngine deps (incl. an old `libwebp.so.6`) so PyInstaller
bundles them, and bakes in `appimagetool`. One image builds either arch — pick
with `--platform` (`build-appimage.sh` names the output per `uname -m`).

```sh
# aarch64 shown; for amd64 swap arm64 -> amd64. Apple Silicon runs arm64
# natively; cross-arch needs qemu once: docker run --privileged --rm tonistiigi/binfmt --install all
docker build --platform=linux/arm64 -t wactorz-appimage:arm64 -f packaging/linux/Dockerfile.appimage .

rm -rf .local/build-venv          # the build venv is per host+arch+python — wipe
                                  # it when switching arch, or away from a native build
docker run --rm --platform=linux/arm64 -v "$PWD:/src" -w /src wactorz-appimage:arm64 \
    bash packaging/linux/build-appimage.sh   # → dist/Wactorz-aarch64.AppImage
```

Then **audit** what the bundle still expects from the host (catches missing-lib
gaps before a tester does — a lib-complete test box won't reveal them):
```sh
docker run --rm --platform=linux/arm64 -v "$PWD:/src" -w /src wactorz-appimage:arm64 \
    bash packaging/linux/audit-appimage.sh dist/Wactorz.AppDir
```
A good result lists **only base libs** (linker, glibc, GL/GPU, DRM, Wayland/xcb)
— those must come from the host and can't be bundled. Anything else (a codec, a
Qt plugin lib, another `libwebp`-style straggler) is a portability risk → add
its package to `Dockerfile.appimage` and rebuild. A clean build also shows no
"Library not found" warnings during the PyInstaller freeze.

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

## macOS (signed + notarized .dmg)
Uses the system Cocoa/WKWebView (no Qt bundled → small), pystray's darwin tray.
Build **on Apple Silicon** (arm64; no cross-build). Prereqs: Xcode CLT, a
"Developer ID Application" cert, and a notarytool keychain profile:
```sh
xcrun notarytool store-credentials wactorz-notary \
    --apple-id <id> --team-id <TEAMID> --password <app-specific-password>
```
Then:
```sh
export MACOS_SIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)"
bash packaging/macos/build-macos.sh        # → dist/Wactorz-<ver>-arm64.dmg
```
Notes:
- `Info.plist` allows local-network http so WKWebView can load the backend
  (`NSAppTransportSecurity → NSAllowsLocalNetworking`).
- Tray: pystray's darwin backend wants the main run loop (pywebview owns it too)
  — verify the tray actually appears; it falls back to none if not.
- `--deep` signing covers PyInstaller's nested dylibs; if notarization flags
  unsigned nested code, sign inner binaries individually.

## Troubleshooting

**"Wactorz backend didn't start" after the loading splash.** The desktop shell
launches the backend as a child process and waits ~30 s for it to answer on
`http://127.0.0.1:8888`. The most common reason it never does is an
**unreachable MQTT broker** — the backend needs its configured MQTT host to be
running and reachable from this machine. Check that first, then reopen.

The child's stdout/stderr is captured to `desktop-backend.log` in the per-user
data dir:

| OS | Log path |
|----|----------|
| Linux / macOS | `~/.local/share/wactorz/desktop-backend.log` (or `$XDG_DATA_HOME/wactorz/`) |
| Windows | `%LOCALAPPDATA%\wactorz\desktop-backend.log` |

**Linux: blank window / no title-bar buttons under GNOME-Wayland.** The bundled
QtWebEngine misbehaves on GNOME's Wayland session; the AppImage forces XWayland
there automatically (`QT_QPA_PLATFORM=xcb`). KDE/others render fine on Wayland.

**Linux: tray appears but no window (weak/quirky GPU).** Qt Quick's GL scene
graph can't get a context on some GPUs (e.g. the Raspberry Pi's VideoCore), so
the window never shows. AppRun falls back to Qt Quick's software rasterizer
automatically on Raspberry Pi; force it anywhere with
`WACTORZ_SOFTWARE_RENDER=1 ./Wactorz-*.AppImage`. GPU-capable machines are
untouched (no perf cost). The web content still uses Chromium's own GPU.

**Linux: choosing a webview backend.** The AppImage bundles Qt. For a `pip`
install pick a backend extra: `wactorz[desktop-qt]` (PySide6/QtWebEngine) or
`wactorz[desktop-gtk]` (system PyGObject + WebKit2GTK, e.g.
`apt install python3-gi gir1.2-webkit2-4.1`). With neither present the app exits
at launch with install instructions.
