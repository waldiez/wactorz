# Desktop App

Wactorz Desktop is a native application built with [Tauri](https://tauri.app) that bundles the
entire Wactorz backend — no Docker, no Python install required.  Open the app, enter your LLM
API key in Settings, and your agents are live.

---

## Download

Pre-built binaries are attached to every [GitHub Release](https://github.com/waldiez/wactorz/releases).

| Platform | File | Notes |
|---|---|---|
| **macOS** (Apple Silicon + Intel) | `Wactorz_*.dmg` | Universal binary |
| **Windows** | `Wactorz_*_x64-setup.exe` or `.msi` | 64-bit |
| **Linux** | `Wactorz_*.AppImage` or `.deb` | AppImage runs without install |

---

## Install

### macOS

1. Open the `.dmg` and drag **Wactorz** to `/Applications`.
2. On first launch macOS may block an unsigned app — right-click → **Open** to bypass Gatekeeper.

### Windows

Run the `.msi` or `_x64-setup.exe` installer; it installs to `%ProgramFiles%\Wactorz`.

### Linux (.AppImage — no install needed)

```bash
chmod +x Wactorz_*.AppImage
./Wactorz_*.AppImage
```

### Linux (.deb — Debian / Ubuntu)

```bash
sudo dpkg -i wactorz_*.deb
```

---

## First launch

1. **Settings** opens automatically if no API key is configured (or press **⌘,** / **Ctrl+,**).
2. Fill in your **LLM provider + API key** (Anthropic, OpenAI, Ollama, Gemini, or NVIDIA NIM).
3. Optionally add MQTT broker and Home Assistant credentials.
4. Click **Save** — then restart the app to apply.

The embedded backend starts in the background on launch; the system tray icon confirms it is
running.  Click the tray icon to show/hide the window.

---

## Features

| Feature | Detail |
|---|---|
| Embedded backend | Rust server starts inside the app process — no separate terminal |
| System tray | Click to show/hide; tooltip shows unread message count |
| Native notifications | OS-level alerts when agents reply while the window is hidden |
| In-app toasts | Animated chat / spawn / alert cards with agent avatars |
| Settings panel | Persistent config stored in the OS keychain-backed app directory |
| Keyboard shortcut | **⌘,** (macOS) / **Ctrl+,** (Windows, Linux) opens Settings |

---

## Building from source

### Prerequisites

| Tool | Version |
|---|---|
| [Rust](https://rustup.rs) | stable (≥ 1.85) |
| [Bun](https://bun.sh) | ≥ 1.3 |
| [Tauri CLI](https://tauri.app/start/create-project/) | v2 (`cargo install tauri-cli`) |
| **Linux only** | `libwebkit2gtk-4.1-dev libayatana-appindicator3-dev librsvg2-dev patchelf` |

### Dev mode (hot-reload)

```bash
cd frontend
bun install
cargo tauri dev
```

The app opens with the frontend served by Vite dev server (hot-reload on TypeScript changes).

### Production build

```bash
cd frontend
bun install
cargo tauri build
```

Output bundles are written to `frontend/src-tauri/target/release/bundle/`.

---

## Ports

The embedded backend picks up `api_port` from Settings (default **8888**).  If you also run the
Docker-based backend locally, use a different port for the desktop app or stop the Docker service
first to avoid conflicts.

---

## Notifications (macOS)

On first launch the app requests notification permission from macOS.  If you dismissed the dialog,
go to **System Settings → Notifications → Wactorz** and set the alert style to **Banners** or
**Alerts**.

> **Note for development builds (`cargo tauri dev`):** notifications appear under
> **Terminal** in System Settings because unsigned dev builds register with
> `com.apple.Terminal` instead of the app bundle ID.  This is a Tauri/notify-rust
> quirk; production builds use `io.waldiez.wactorz`.
