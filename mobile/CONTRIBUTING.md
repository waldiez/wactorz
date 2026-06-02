# Contributing to the Mobile App

## Adding a new screen

1. Create `lib/screens/my_screen.dart` with a `StatelessWidget` (or `StatefulWidget` if the screen has local UI state).
2. Add the route to `lib/screens/home.dart` (tab) or navigate to it imperatively with `Navigator.push`.
3. Read global state through `context.watch<WactorzClient>()` — never hold a separate copy of agent/feed data.
4. If the screen needs its own async data that isn't in `WactorzClient`, fetch it in `initState` / `didChangeDependencies` and store it in local `State` — don't add it to `WactorzClient` unless other screens also need it.

## Provider pattern rules

- All shared state lives in `WactorzClient` or `TtsService` — both are `ChangeNotifier`s registered in `main.dart`'s `MultiProvider`.
- Call `notifyListeners()` after every mutation in a `ChangeNotifier`.
- Use `context.read<T>()` in callbacks/handlers (fire-and-forget), `context.watch<T>()` in `build()` (reactive).
- Never call `context.watch` inside a callback — it will throw at runtime.
- Avoid nested `ChangeNotifierProvider`s; if a new piece of shared state is needed, add it to `main.dart`.

## Code style

- Follow the existing file naming: `snake_case.dart` for files, `PascalCase` for classes.
- Prefer `const` constructors wherever possible.
- Keep `build()` methods short — extract sub-widgets into private `_MyWidget` classes or top-level widget functions in the same file.
- Run `flutter analyze` before committing — the project uses `analysis_options.yaml` with recommended lint rules.
- Format with `dart format lib/` (or `flutter format .`).

## Widgets

Reusable widgets live in `lib/widgets/`:

| Widget | File | Purpose |
|---|---|---|
| `AgentCard` | `agent_card.dart` | Agent list tile with status and action buttons |
| `StatusDot` | `status_dot.dart` | Colour-coded status indicator |
| `VoiceButton` | `voice_button.dart` | Mic button wired to `speech_to_text` |

Add new shared widgets here; screen-specific widgets can stay in the screen file as private classes.

## Adding a dependency

```bash
flutter pub add <package>
```

Then document the package's purpose in the table in `README.md`.

Avoid packages with native plugins unless there's no pure-Dart alternative — native plugins increase the build matrix and can block web/desktop targets.

## Testing

Unit and widget tests live in `test/`. Run them with:

```bash
flutter test
```

- Widget tests: use `flutter_test` + `pumpWidget`. Mock `WactorzClient` with a minimal `ChangeNotifier` subclass.
- Don't test layout pixel-perfectly — test behaviour (button tap → method called, data shown).
- For WebSocket/HTTP logic in `WactorzClient`, test the parsing and state-update methods directly without a real server.

## Updating assets

Add new images/fonts to `assets/` and declare them in `pubspec.yaml` under `flutter: assets:`. Run `flutter pub get` after editing `pubspec.yaml`.
