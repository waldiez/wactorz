# Packaging — Linux desktop

Builds the `wactorz-desktop` pywebview app into three Linux formats. Two
distinct strategies share one codebase via the `WACTORZ_WEBVIEW_GUI` env var
(see `wactorz/desktop/app.py`):

| Format | Strategy | Webview | Size | Portability |
|--------|----------|---------|------|-------------|
| **AppImage** | PyInstaller frozen, self-contained | bundled Qt/QtWebEngine | large (~300 MB+) | runs anywhere |
| **.deb / .rpm** | venv in `/opt/wactorz` + declared deps | host WebKit2GTK | small | needs distro WebKit2GTK 4.1 |

## Layout
```
packaging/
  pyinstaller/wactorz-desktop.spec   # onedir freeze (Qt) — AppImage only
  appimage/AppRun                    # forces gui=qt, disables QtWebEngine sandbox
  appimage/wactorz.desktop
  appimage/build-appimage.sh         # pyinstaller → AppDir → appimagetool
  linux/wactorz.desktop              # deb/rpm desktop entry
  linux/build-deb-rpm.sh             # stage venv (no PySide6) → fpm x2
  build-all.sh
```

## Prerequisites
- **AppImage**: `pip install pyinstaller`, plus `appimagetool` on `PATH`
  (or `APPIMAGETOOL=/path/to/appimagetool`). Build on the oldest glibc you
  support (e.g. an Ubuntu 22.04 container).
- **deb/rpm**: `python3 >= 3.10` and [`fpm`](https://fpm.readthedocs.io).
  Build `.deb` on Debian/Ubuntu and `.rpm` on Fedora.

## Build
```sh
ARCH=x86_64 bash packaging/appimage/build-appimage.sh   # → dist/Wactorz-x86_64.AppImage
bash packaging/linux/build-deb-rpm.sh                   # → dist/*.deb, dist/*.rpm
# or everything on the current host:
bash packaging/build-all.sh
```

## Notes / TODO
- Runtime-test both paths: the GTK path (deb/rpm) is what runs today on dev
  laptops; the Qt/AppImage path needs its own smoke test (sandbox flag).
- deb/rpm assume **WebKit2GTK 4.1** (`gir1.2-webkit2gtk-4.1` / `webkit2gtk4.1`).
  Older LTS distros ship 4.0 (different gir name) — pin supported distros.
- `aarch64` AppImage: build on an arm64 host with `ARCH=aarch64` and an
  aarch64 `appimagetool`.
- A `make package-linux` target wiring these in is a sensible follow-up.
