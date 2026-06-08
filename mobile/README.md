# Wactorz Mobile

Flutter companion app for Wactorz. Connects to a running Wactorz server over WebSocket and HTTP, lets you monitor agents, chat with them, and watch the live event feed.

## Requirements

- Flutter SDK ≥ 3.x (`flutter --version`)
- Android SDK (for Android builds) or Xcode (for iOS builds)
- A running Wactorz server reachable from the device

## Quick start

```bash
cd mobile
flutter pub get
flutter run          # picks up a connected device or emulator
```

On first launch you'll see the **Setup** screen — enter the Wactorz server URL (e.g. `http://192.168.1.10:8000`) and tap Connect. The URL is saved to SharedPreferences and remembered across sessions.

## Screen map

| Screen | File | Description |
|---|---|---|
| Setup | `lib/screens/setup.dart` | URL entry on first launch or after disconnect |
| Home | `lib/screens/home.dart` | 3-tab shell: Agents / Chat / Feed |
| Agents | `lib/screens/agents.dart` | Live list of agents with status indicators |
| Chat | `lib/screens/chat.dart` | Per-agent conversation with voice input support |
| Global Chat | `lib/screens/global_chat.dart` | Broadcast chat sent to all agents |
| Feed | `lib/screens/feed.dart` | Timestamped event stream from the server |

## Architecture

```
main.dart
 └─ MultiProvider
     ├─ WactorzClient   (lib/client.dart)   — WebSocket + HTTP, reconnect logic
     └─ TtsService      (lib/services/tts_service.dart) — audio playback
```

`WactorzClient` is a `ChangeNotifier`. All screens call `context.watch<WactorzClient>()` to rebuild when state changes. The WebSocket connection is established after the server URL is saved; HTTP is used for one-off requests (agent list, config).

### Key dependencies (pubspec.yaml)

| Package | Purpose |
|---|---|
| `web_socket_channel` | WebSocket connection to Wactorz |
| `http` | REST calls (agent list, chat POST) |
| `provider` | State management (`ChangeNotifier`) |
| `flutter_markdown` | Render agent responses as Markdown |
| `speech_to_text` | Voice input in Chat screen |
| `audioplayers` | TTS audio playback |
| `google_fonts` | Typography |
| `timeago` | Relative timestamps in feeds |
| `intl` | Date and number formatting |
| `cupertino_icons` | iOS-style icon set |
| `shared_preferences` | Persist server URL |

## Building for release

```bash
# Android APK
flutter build apk --release

# Android App Bundle (for Play Store)
flutter build appbundle --release

# iOS (requires macOS + Xcode)
flutter build ios --release
```

## Assets

Static assets (icons, images) live in `assets/`. The current declared asset is `assets/icon.png` under `flutter: assets:`.
