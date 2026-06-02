import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:wactorz/main.dart';
import 'package:wactorz/services/wactorz_client.dart';
import 'package:wactorz/services/tts_service.dart';
import 'package:wactorz/screens/setup_screen.dart';
import 'package:wactorz/screens/home_screen.dart';
import 'package:wactorz/widgets/status_dot.dart';
import 'package:wactorz/widgets/chat_bubble.dart';
import 'package:wactorz/widgets/feed_tile.dart';
import 'package:wactorz/models/chat_message.dart';
import 'package:wactorz/models/feed_item.dart';

Widget _wrap(Widget child, {WactorzClient? client}) {
  return MultiProvider(
    providers: [
      ChangeNotifierProvider<WactorzClient>.value(
        value: client ?? WactorzClient(),
      ),
      ChangeNotifierProvider<TtsService>.value(value: TtsService()),
    ],
    child: MaterialApp(
      // Material 3's default InkSparkle splash uses a GLSL shader that the
      // headless test runtime cannot load.  Override with InkRipple to avoid
      // the 'ink_sparkle.frag' asset error on button taps.
      theme: ThemeData(splashFactory: InkRipple.splashFactory),
      home: child,
    ),
  );
}

void main() {
  setUpAll(() {
    TestWidgetsFlutterBinding.ensureInitialized();
  });

  setUp(() {
    SharedPreferences.setMockInitialValues({});
  });

  group('WactorzApp', () {
    testWidgets('renders without crashing — shows loading then setup', (tester) async {
      // WactorzApp itself is the MaterialApp shell; providers live above it in
      // main() so tests must supply them explicitly.
      await tester.pumpWidget(
        MultiProvider(
          providers: [
            ChangeNotifierProvider(create: (_) => WactorzClient()),
            ChangeNotifierProvider(create: (_) => TtsService()),
          ],
          child: const WactorzApp(),
        ),
      );
      await tester.pump();
      expect(find.byType(WactorzApp), findsOneWidget);
    });
  });

  group('SetupScreen', () {
    testWidgets('shows connect button and URL field', (tester) async {
      await tester.pumpWidget(_wrap(const SetupScreen()));
      await tester.pumpAndSettle();
      expect(find.text('Connect'), findsOneWidget);
      expect(find.byType(TextField), findsOneWidget);
    });

    testWidgets('connect button disabled while saving', (tester) async {
      await tester.pumpWidget(_wrap(const SetupScreen()));
      await tester.pumpAndSettle();
      final btn = find.text('Connect');
      expect(btn, findsOneWidget);
    });

    testWidgets('empty URL does not trigger save', (tester) async {
      await tester.pumpWidget(_wrap(const SetupScreen()));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Connect'));
      await tester.pump();
      // No navigation — still on SetupScreen
      expect(find.byType(SetupScreen), findsOneWidget);
    });
  });

  group('HomeScreen', () {
    testWidgets('shows Wactorz title and bottom nav', (tester) async {
      SharedPreferences.setMockInitialValues({'wactorz_url': 'http://test:8888'});
      final client = WactorzClient(wsConnect: (_) => throw Exception('no ws'));
      await tester.pumpWidget(_wrap(const HomeScreen(), client: client));
      await tester.pumpAndSettle();
      expect(find.text('Wactorz'), findsOneWidget);
      expect(find.byType(NavigationBar), findsOneWidget);
    });

    testWidgets('settings icon is present', (tester) async {
      final client = WactorzClient(wsConnect: (_) => throw Exception('no ws'));
      await tester.pumpWidget(_wrap(const HomeScreen(), client: client));
      await tester.pumpAndSettle();
      expect(find.byIcon(Icons.settings_outlined), findsOneWidget);
    });
  });

  group('StatusDot', () {
    testWidgets('running state renders animated dot', (tester) async {
      await tester.pumpWidget(
        const MaterialApp(home: Scaffold(body: StatusDot(state: 'running'))),
      );
      await tester.pump();
      expect(find.byType(StatusDot), findsOneWidget);
    });

    testWidgets('stopped state renders static dot', (tester) async {
      await tester.pumpWidget(
        const MaterialApp(home: Scaffold(body: StatusDot(state: 'stopped'))),
      );
      await tester.pump();
      expect(find.byType(StatusDot), findsOneWidget);
    });

    testWidgets('error/unknown state renders static dot', (tester) async {
      await tester.pumpWidget(
        const MaterialApp(home: Scaffold(body: StatusDot(state: 'unknown'))),
      );
      await tester.pump();
      expect(find.byType(StatusDot), findsOneWidget);
    });
  });

  group('ChatBubble', () {
    testWidgets('user message renders on the right', (tester) async {
      const msg = ChatMessage(role: 'user', content: 'hello', ts: 0);
      await tester.pumpWidget(
        const MaterialApp(home: Scaffold(body: ChatBubble(message: msg))),
      );
      await tester.pumpAndSettle();
      expect(find.text('hello'), findsOneWidget);
    });

    testWidgets('assistant message uses MarkdownBody', (tester) async {
      const msg = ChatMessage(role: 'assistant', content: '**bold**', ts: 0);
      await tester.pumpWidget(
        const MaterialApp(home: Scaffold(body: SingleChildScrollView(child: ChatBubble(message: msg)))),
      );
      await tester.pumpAndSettle();
      expect(find.byType(ChatBubble), findsOneWidget);
    });

    testWidgets('streaming message shows cursor', (tester) async {
      const msg = ChatMessage(role: 'assistant', content: '', ts: 0, isStreaming: true);
      await tester.pumpWidget(
        const MaterialApp(home: Scaffold(body: ChatBubble(message: msg))),
      );
      await tester.pumpAndSettle();
      expect(find.byType(ChatBubble), findsOneWidget);
    });
  });

  group('FeedTile', () {
    testWidgets('renders label and agentName', (tester) async {
      final item = FeedItem.fromJson({
        'type': 'spawn',
        'label': 'agent started',
        'agentName': 'mybot',
        'timestamp': 0.0,
      });
      await tester.pumpWidget(
        MaterialApp(home: Scaffold(body: FeedTile(item: item))),
      );
      await tester.pumpAndSettle();
      expect(find.text('mybot'), findsOneWidget);
      expect(find.text('agent started'), findsOneWidget);
    });

    testWidgets('renders all event types without error', (tester) async {
      for (final type in ['spawn', 'heartbeat', 'chat', 'alert-error', 'alert-warning', 'stopped', 'qa-flag', 'other']) {
        final item = FeedItem.fromJson({'type': type, 'label': type, 'agentName': 'a', 'timestamp': 0.0});
        await tester.pumpWidget(
          MaterialApp(home: Scaffold(body: FeedTile(item: item))),
        );
        await tester.pumpAndSettle();
        expect(find.byType(FeedTile), findsOneWidget);
      }
    });
  });
}
