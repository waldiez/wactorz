import 'dart:async';
import 'dart:convert';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:web_socket_channel/web_socket_channel.dart';
import 'package:wactorz/services/wactorz_client.dart';

// ── Fake WebSocket channel ────────────────────────────────────────────────────

class _FakeWsChannel extends Fake implements WebSocketChannel {
  final _ctrl = StreamController<dynamic>.broadcast();
  final List<dynamic> sent = [];
  bool sinkClosed = false;

  @override
  Stream<dynamic> get stream => _ctrl.stream;

  @override
  WebSocketSink get sink => _FakeSink(sent, onClose: () => sinkClosed = true);

  void push(dynamic msg) => _ctrl.add(msg);
  Future<void> finish() => _ctrl.close();
}

class _FakeSink implements WebSocketSink {
  final List<dynamic> sent;
  final void Function() onClose;
  _FakeSink(this.sent, {required this.onClose});

  @override
  void add(dynamic data) => sent.add(data);

  @override
  Future<void> close([int? closeCode, String? closeReason]) async => onClose();

  @override
  void addError(Object error, [StackTrace? stackTrace]) {}

  @override
  Future<void> addStream(Stream<dynamic> stream) async {}

  @override
  Future<void> get done => Future.value();
}

// ── No-op channel for tests that don't need WS messages ──────────────────────

class _NoopWsChannel extends Fake implements WebSocketChannel {
  final _ctrl = StreamController<dynamic>();
  @override
  Stream<dynamic> get stream => _ctrl.stream;
  @override
  WebSocketSink get sink => _FakeSink([], onClose: () {});
}

// ── Helpers ──────────────────────────────────────────────────────────────────

WactorzClient _makeClient({
  WebSocketChannel Function(Uri)? ws,
  http.Client? httpClient,
}) =>
    WactorzClient(
      wsConnect: ws ?? (_) => _NoopWsChannel(),
      httpClient: httpClient,
    );

String _snapshot({
  List<Map<String, dynamic>> agents = const [],
  List<Map<String, dynamic>> feed = const [],
  double cost = 0,
  int messages = 0,
}) =>
    jsonEncode({
      'type': 'full_snapshot',
      'state': {
        'agents': agents,
        'log_feed': feed,
        'total_cost_usd': cost,
        'total_messages': messages,
      },
    });

// ── Tests ─────────────────────────────────────────────────────────────────────

void main() {
  setUpAll(() {
    TestWidgetsFlutterBinding.ensureInitialized();
  });

  setUp(() {
    SharedPreferences.setMockInitialValues({});
  });

  // ── URL handling ─────────────────────────────────────────────────────────

  group('setUrl', () {
    test('normalises trailing slash', () async {
      final c = _makeClient();
      await c.setUrl('http://host:8888/');
      expect(c.baseUrl, 'http://host:8888');
      c.dispose();
    });

    test('persists to SharedPreferences', () async {
      final c = _makeClient();
      await c.setUrl('http://host:8888');
      final prefs = await SharedPreferences.getInstance();
      expect(prefs.getString('wactorz_url'), 'http://host:8888');
      c.dispose();
    });

    test('triggers connect (state becomes connected or connecting)', () async {
      final c = _makeClient();
      await c.setUrl('http://host:8888');
      expect(c.connState, isNot(WsState.disconnected));
      c.dispose();
    });
  });

  group('init', () {
    test('loads saved URL and connects', () async {
      SharedPreferences.setMockInitialValues({'wactorz_url': 'http://saved:8888'});
      final c = _makeClient();
      await c.init();
      expect(c.baseUrl, 'http://saved:8888');
      expect(c.connState, isNot(WsState.disconnected));
      c.dispose();
    });

    test('does nothing when no saved URL', () async {
      final c = _makeClient();
      await c.init();
      expect(c.baseUrl, '');
      expect(c.connState, WsState.disconnected);
      c.dispose();
    });
  });

  // ── WS message processing ─────────────────────────────────────────────────

  group('processMessage — full_snapshot', () {
    test('populates agents from camelCase keys', () {
      final c = _makeClient();
      c.processMessage(_snapshot(agents: [
        {'id': 'a1', 'name': 'alpha', 'state': 'running', 'messagesProcessed': 3, 'costUsd': 0.001},
      ]));
      expect(c.agents.length, 1);
      expect(c.agents[0].name, 'alpha');
      expect(c.agents[0].isRunning, true);
      expect(c.agents[0].messagesProcessed, 3);
      c.dispose();
    });

    test('populates agents from snake_case keys (raw snapshot)', () {
      final c = _makeClient();
      c.processMessage(_snapshot(agents: [
        {'agent_id': 'b1', 'name': 'beta', 'state': 'stopped', 'messages_processed': 7, 'cost_usd': 0.02},
      ]));
      expect(c.agents.length, 1);
      expect(c.agents[0].id, 'b1');
      expect(c.agents[0].messagesProcessed, 7);
      c.dispose();
    });

    test('replaces agent list on each snapshot', () {
      final c = _makeClient();
      c.processMessage(_snapshot(agents: [
        {'agent_id': 'x', 'name': 'first'},
        {'agent_id': 'y', 'name': 'second'},
      ]));
      expect(c.agents.length, 2);
      c.processMessage(_snapshot(agents: [
        {'agent_id': 'z', 'name': 'only'},
      ]));
      expect(c.agents.length, 1);
      expect(c.agents[0].name, 'only');
      c.dispose();
    });

    test('updates totalCostUsd and totalMessages', () {
      final c = _makeClient();
      c.processMessage(_snapshot(cost: 1.23, messages: 42));
      expect(c.totalCostUsd, 1.23);
      expect(c.totalMessages, 42);
      c.dispose();
    });

    test('deduplicates feed items by timestamp+label', () {
      final c = _makeClient();
      final feedItem = {'type': 'spawn', 'label': 'started', 'agentName': 'a', 'timestamp': 1.0};
      c.processMessage(_snapshot(feed: [feedItem]));
      c.processMessage(_snapshot(feed: [feedItem]));
      expect(c.feed.length, 1);
      c.dispose();
    });

    test('caps feed at 500 items', () {
      final c = _makeClient();
      final items = List.generate(
        600,
        (i) => {'type': 'chat', 'label': 'msg$i', 'agentName': 'a', 'timestamp': i.toDouble()},
      );
      c.processMessage(_snapshot(feed: items));
      expect(c.feed.length, lessThanOrEqualTo(500));
      c.dispose();
    });
  });

  group('processMessage — patch', () {
    test('patch applies same as full_snapshot', () {
      final c = _makeClient();
      c.processMessage(jsonEncode({
        'type': 'patch',
        'state': {
          'agents': [{'agent_id': 'p1', 'name': 'patched'}],
          'log_feed': [],
          'total_cost_usd': 0,
          'total_messages': 0,
        },
      }));
      expect(c.agents.length, 1);
      expect(c.agents[0].name, 'patched');
      c.dispose();
    });
  });

  group('processMessage — reset', () {
    test('reset applies new state snapshot', () {
      final c = _makeClient();
      c.processMessage(_snapshot(agents: [
        {'agent_id': 'old', 'name': 'before'},
      ]));
      expect(c.agents.length, 1);

      c.processMessage(jsonEncode({
        'type': 'reset',
        'scope': 'all',
        'state': {
          'agents': [],
          'log_feed': [],
          'total_cost_usd': 0,
          'total_messages': 0,
        },
      }));
      expect(c.agents.isEmpty, true);
      c.dispose();
    });
  });

  group('processMessage — delete_agent', () {
    test('removes agent by id', () {
      final c = _makeClient();
      c.processMessage(_snapshot(agents: [
        {'agent_id': 'del1', 'name': 'gone'},
        {'agent_id': 'del2', 'name': 'stays'},
      ]));
      expect(c.agents.length, 2);

      c.processMessage(jsonEncode({'type': 'delete_agent', 'agent_id': 'del1'}));
      expect(c.agents.length, 1);
      expect(c.agents[0].name, 'stays');
      c.dispose();
    });

    test('no-op when agent_id not found', () {
      final c = _makeClient();
      c.processMessage(_snapshot(agents: [{'agent_id': 'x', 'name': 'x'}]));
      c.processMessage(jsonEncode({'type': 'delete_agent', 'agent_id': 'nope'}));
      expect(c.agents.length, 1);
      c.dispose();
    });
  });

  group('processMessage — streaming', () {
    test('stream_chunk builds buffer', () {
      final c = _makeClient();
      c.setActiveChatAgent('bot');

      c.processMessage(jsonEncode({'type': 'stream_chunk', 'content': 'hel', 'timestamp': 1.0}));
      expect(c.streamBuffer?.content, 'hel');
      expect(c.streamBuffer?.isStreaming, true);

      c.processMessage(jsonEncode({'type': 'stream_chunk', 'content': 'lo', 'timestamp': 1.0}));
      expect(c.streamBuffer?.content, 'hello');
      c.dispose();
    });

    test('stream_end finalizes and clears buffer', () {
      final c = _makeClient();
      c.setActiveChatAgent('bot');
      c.processMessage(jsonEncode({'type': 'stream_chunk', 'content': 'hi', 'timestamp': 1.0}));
      expect(c.streamBuffer, isNotNull);

      c.processMessage(jsonEncode({'type': 'stream_end', 'timestamp': 1.0}));
      expect(c.streamBuffer, isNull);
      final msgs = c.messagesFor('bot');
      expect(msgs.length, 1);
      expect(msgs[0].content, 'hi');
      expect(msgs[0].isStreaming, false);
      c.dispose();
    });

    test('stream_end without active agent is a no-op', () {
      final c = _makeClient();
      c.processMessage(jsonEncode({'type': 'stream_chunk', 'content': 'x', 'timestamp': 1.0}));
      c.processMessage(jsonEncode({'type': 'stream_end', 'timestamp': 1.0}));
      // No active agent set — buffer cleared but nothing stored
      expect(c.streamBuffer, isNull);
      c.dispose();
    });
  });

  group('processMessage — invalid input', () {
    test('malformed JSON does not throw', () {
      final c = _makeClient();
      expect(() => c.processMessage('not json'), returnsNormally);
      c.dispose();
    });

    test('unknown type is ignored', () {
      final c = _makeClient();
      c.processMessage(jsonEncode({'type': 'bogus', 'data': 123}));
      expect(c.agents.isEmpty, true);
      c.dispose();
    });

    test('missing type field is ignored', () {
      final c = _makeClient();
      c.processMessage(jsonEncode({'content': 'orphan'}));
      expect(c.agents.isEmpty, true);
      c.dispose();
    });
  });

  // ── sendMessage ───────────────────────────────────────────────────────────

  group('sendMessage', () {
    test('adds user message to local history and sends over WS', () async {
      SharedPreferences.setMockInitialValues({'wactorz_url': 'http://test:8888'});
      final ws = _FakeWsChannel();
      final c = WactorzClient(wsConnect: (_) => ws);
      await c.init();

      c.setActiveChatAgent('bot');
      c.sendMessage('hello', toAgent: 'bot');

      expect(c.messagesFor('bot').length, 1);
      expect(c.messagesFor('bot')[0].role, 'user');
      expect(c.messagesFor('bot')[0].content, 'hello');
      expect(ws.sent.length, 1);
      final sent = jsonDecode(ws.sent[0] as String) as Map<String, dynamic>;
      expect(sent['type'], 'chat');
      expect(sent['content'], 'hello');
      expect(sent['to'], 'bot');

      c.dispose();
      await ws.finish();
    });

    test('empty / whitespace content is ignored', () async {
      SharedPreferences.setMockInitialValues({'wactorz_url': 'http://test:8888'});
      final ws = _FakeWsChannel();
      final c = WactorzClient(wsConnect: (_) => ws);
      await c.init();
      c.sendMessage('  ');
      expect(ws.sent, isEmpty);
      c.dispose();
      await ws.finish();
    });

    test('uses activeChatAgent as fallback target', () async {
      SharedPreferences.setMockInitialValues({'wactorz_url': 'http://test:8888'});
      final ws = _FakeWsChannel();
      final c = WactorzClient(wsConnect: (_) => ws);
      await c.init();
      c.setActiveChatAgent('fallback');
      c.sendMessage('hi');
      expect(c.messagesFor('fallback').length, 1);
      c.dispose();
      await ws.finish();
    });
  });

  // ── chat message type ─────────────────────────────────────────────────────

  group('processMessage — chat', () {
    test('chat message with active agent finalizes into history', () {
      final c = _makeClient();
      c.setActiveChatAgent('bot');
      c.processMessage(jsonEncode({'type': 'chat', 'content': 'reply', 'timestamp': 1.0}));
      expect(c.messagesFor('bot').length, 1);
      expect(c.messagesFor('bot')[0].content, 'reply');
      expect(c.messagesFor('bot')[0].isStreaming, false);
      c.dispose();
    });

    test('chat message without active agent discards buffer', () {
      final c = _makeClient();
      c.processMessage(jsonEncode({'type': 'chat', 'content': 'orphan', 'timestamp': 1.0}));
      // _finalizeStream always clears the buffer; no active agent means the
      // message is discarded rather than stored under an unknown agent.
      expect(c.streamBuffer, isNull);
      c.dispose();
    });
  });

  group('messagesFor', () {
    test('returns empty list for unknown agent', () {
      final c = _makeClient();
      expect(c.messagesFor('nobody'), isEmpty);
      c.dispose();
    });

    test('returns stored messages after stream finalize', () {
      final c = _makeClient();
      c.setActiveChatAgent('agt');
      c.processMessage(jsonEncode({'type': 'stream_chunk', 'content': 'reply', 'timestamp': 1.0}));
      c.processMessage(jsonEncode({'type': 'stream_end', 'timestamp': 1.0}));
      expect(c.messagesFor('agt').length, 1);
      expect(c.messagesFor('agt')[0].content, 'reply');
      c.dispose();
    });
  });

  // ── HTTP — loadChatHistory ────────────────────────────────────────────────

  group('loadChatHistory', () {
    test('parses response and stores messages', () async {
      final mockHttp = MockClient((req) async {
        expect(req.url.path, '/api/chats');
        expect(req.url.queryParameters['agent'], 'test-agent');
        return http.Response(
          jsonEncode([
            {'role': 'user', 'content': 'hello', 'ts': 1700000000.0},
            {'role': 'assistant', 'content': 'hi', 'ts': 1700000001.0},
          ]),
          200,
        );
      });
      SharedPreferences.setMockInitialValues({'wactorz_url': 'http://test:8888'});
      final c = _makeClient(httpClient: mockHttp);
      await c.init();
      await c.loadChatHistory('test-agent');

      final msgs = c.messagesFor('test-agent');
      expect(msgs.length, 2);
      expect(msgs[0].role, 'user');
      expect(msgs[1].role, 'assistant');
      c.dispose();
    });

    test('silently ignores non-200 response', () async {
      final mockHttp = MockClient((_) async => http.Response('error', 500));
      SharedPreferences.setMockInitialValues({'wactorz_url': 'http://test:8888'});
      final c = _makeClient(httpClient: mockHttp);
      await c.init();
      await c.loadChatHistory('agent');
      expect(c.messagesFor('agent'), isEmpty);
      c.dispose();
    });

    test('silently ignores network error', () async {
      final mockHttp = MockClient((_) => Future.error(Exception('offline')));
      SharedPreferences.setMockInitialValues({'wactorz_url': 'http://test:8888'});
      final c = _makeClient(httpClient: mockHttp);
      await c.init();
      await c.loadChatHistory('agent');
      expect(c.messagesFor('agent'), isEmpty);
      c.dispose();
    });

    test('no-op when baseUrl is empty', () async {
      final c = _makeClient();
      await c.loadChatHistory('agent');
      expect(c.messagesFor('agent'), isEmpty);
      c.dispose();
    });
  });

  // ── connect / WS lifecycle ─────────────────────────────────────────────────

  group('connect', () {
    test('no-op when baseUrl is empty', () {
      final c = _makeClient();
      c.connect();
      expect(c.connState, WsState.disconnected);
      c.dispose();
    });

    test('uses wsConnect factory with correct URI', () async {
      Uri? capturedUri;
      final ws = _NoopWsChannel();
      final c = WactorzClient(wsConnect: (uri) {
        capturedUri = uri;
        return ws;
      });
      SharedPreferences.setMockInitialValues({'wactorz_url': 'http://host:8888'});
      await c.init();
      expect(capturedUri?.toString(), 'ws://host:8888/ws');
      expect(c.connState, WsState.connected);
      c.dispose();
    });

    test('converts https to wss', () async {
      Uri? capturedUri;
      final c = WactorzClient(wsConnect: (uri) {
        capturedUri = uri;
        return _NoopWsChannel();
      });
      SharedPreferences.setMockInitialValues({'wactorz_url': 'https://secure:443'});
      await c.init();
      expect(capturedUri?.scheme, 'wss');
      c.dispose();
    });
  });

  group('WS error / done callbacks', () {
    test('error sets error state', () async {
      final ws = _FakeWsChannel();
      final c = WactorzClient(wsConnect: (_) => ws);
      SharedPreferences.setMockInitialValues({'wactorz_url': 'http://host:8888'});
      await c.init();
      expect(c.connState, WsState.connected);

      ws._ctrl.addError(Exception('connection failed'));
      await Future.delayed(Duration.zero);
      expect(c.connState, WsState.error);
      expect(c.errorMessage, contains('connection failed'));
      c.dispose();
      await ws.finish();
    });

    test('done sets disconnected state', () async {
      final ws = _FakeWsChannel();
      final c = WactorzClient(wsConnect: (_) => ws);
      SharedPreferences.setMockInitialValues({'wactorz_url': 'http://host:8888'});
      await c.init();
      expect(c.connState, WsState.connected);

      await ws.finish();
      await Future.delayed(Duration.zero);
      expect(c.connState, WsState.disconnected);
      c.dispose();
    });
  });

  // ── setActiveChatAgent / streamBuffer ─────────────────────────────────────

  group('setActiveChatAgent', () {
    test('sets the active agent used for stream finalization', () {
      final c = _makeClient();
      c.setActiveChatAgent('mybot');
      c.processMessage(jsonEncode({'type': 'stream_chunk', 'content': 'A', 'timestamp': 0.0}));
      c.processMessage(jsonEncode({'type': 'stream_end', 'timestamp': 0.0}));
      expect(c.messagesFor('mybot').length, 1);
      c.dispose();
    });

    test('null clears active agent (stream_end becomes no-op)', () {
      final c = _makeClient();
      c.setActiveChatAgent('bot');
      c.setActiveChatAgent(null);
      c.processMessage(jsonEncode({'type': 'stream_chunk', 'content': 'X', 'timestamp': 0.0}));
      c.processMessage(jsonEncode({'type': 'stream_end', 'timestamp': 0.0}));
      expect(c.messagesFor('bot'), isEmpty);
      c.dispose();
    });
  });
}
