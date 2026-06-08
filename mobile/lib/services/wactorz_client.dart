import 'dart:async';
import 'dart:convert';
import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';
import 'package:web_socket_channel/web_socket_channel.dart';
import '../models/agent.dart';
import '../models/feed_item.dart';
import '../models/chat_message.dart';

enum WsState { disconnected, connecting, connected, error }

class WactorzClient extends ChangeNotifier {
  final http.Client _http;
  final WebSocketChannel Function(Uri)? _wsConnect;

  WactorzClient({
    http.Client? httpClient,
    WebSocketChannel Function(Uri)? wsConnect,
  })  : _http = httpClient ?? http.Client(),
        _wsConnect = wsConnect;

  String _baseUrl = '';
  String get baseUrl => _baseUrl;

  WsState _connState = WsState.disconnected;
  WsState get connState => _connState;

  String? _errorMessage;
  String? get errorMessage => _errorMessage;

  final List<Agent> _agents = [];
  List<Agent> get agents => List.unmodifiable(_agents);

  final List<FeedItem> _feed = [];
  List<FeedItem> get feed => List.unmodifiable(_feed);

  final Map<String, List<ChatMessage>> _chats = {};

  double totalCostUsd = 0;
  int totalMessages = 0;

  WebSocketChannel? _ws;
  StreamSubscription? _wsSub;
  Timer? _reconnectTimer;

  static const _urlKey = 'wactorz_url';

  Future<void> init() async {
    final prefs = await SharedPreferences.getInstance();
    final saved = prefs.getString(_urlKey);
    if (saved != null && saved.isNotEmpty) {
      _baseUrl = saved;
      connect();
    }
  }

  Future<bool> setUrl(String url) async {
    final normalized = url.endsWith('/') ? url.substring(0, url.length - 1) : url;
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(_urlKey, normalized);
    _baseUrl = normalized;
    await _disconnect();
    connect();
    return true;
  }

  void connect() {
    if (_baseUrl.isEmpty) return;
    _setConnState(WsState.connecting);
    _errorMessage = null;

    try {
      final wsUrl = _baseUrl
          .replaceFirst('https://', 'wss://')
          .replaceFirst('http://', 'ws://');
      final uri = Uri.parse('$wsUrl/ws');
      _ws = (_wsConnect ?? WebSocketChannel.connect)(uri);
      _wsSub = _ws!.stream.listen(
        _onWsMessage,
        onError: _onWsError,
        onDone: _onWsDone,
      );
      _setConnState(WsState.connected);
    } catch (e) {
      _setConnState(WsState.error);
      _errorMessage = e.toString();
      _scheduleReconnect();
    }
  }

  void _onWsMessage(dynamic raw) {
    try {
      final data = jsonDecode(raw as String) as Map<String, dynamic>;
      final type = data['type'] as String?;

      switch (type) {
        case 'full_snapshot':
        case 'patch':
        case 'reset':
          final state = data['state'] as Map<String, dynamic>?;
          if (state != null) _applySnapshot(state);

        case 'delete_agent':
          final id = data['agent_id'] as String?;
          if (id != null) {
            _agents.removeWhere((a) => a.id == id);
            notifyListeners();
          }

        case 'chat':
          _handleChatMsg(data, isStreaming: false);

        case 'stream_chunk':
          _handleStreamChunk(data);

        case 'stream_end':
          _finalizeStream();
      }
    } catch (e) {
      debugPrint('[WS] Failed to parse message: $e');
    }
  }

  /// Exposes [_onWsMessage] for unit testing.
  @visibleForTesting
  void processMessage(dynamic raw) => _onWsMessage(raw);

  void _applySnapshot(Map<String, dynamic> state) {
    final rawAgents = state['agents'] as List? ?? [];
    _agents.clear();
    for (final a in rawAgents) {
      if (a is Map<String, dynamic>) _agents.add(Agent.fromJson(a));
    }

    final rawFeed = state['log_feed'] as List? ?? [];
    for (final f in rawFeed) {
      if (f is Map<String, dynamic>) {
        final item = FeedItem.fromJson(f);
        if (!_feed.any((e) => e.timestamp == item.timestamp && e.label == item.label)) {
          _feed.insert(0, item);
        }
      }
    }
    if (_feed.length > 500) _feed.removeRange(500, _feed.length);

    totalCostUsd = (state['total_cost_usd'] as num?)?.toDouble() ?? 0;
    totalMessages = (state['total_messages'] as num?)?.toInt() ?? 0;
    notifyListeners();
  }

  void _handleChatMsg(Map<String, dynamic> data, {required bool isStreaming}) {
    final content = data['content'] as String? ?? '';
    final ts = (data['timestamp'] as num?)?.toDouble() ?? 0.0;
    _streamBuffer ??= ChatMessage(role: 'assistant', content: content, ts: ts, isStreaming: isStreaming);
    if (!isStreaming) {
      _finalizeStream();
    }
    notifyListeners();
  }

  void _handleStreamChunk(Map<String, dynamic> data) {
    final chunk = data['content'] as String? ?? '';
    final ts = (data['timestamp'] as num?)?.toDouble() ?? 0.0;
    if (_streamBuffer == null) {
      _streamBuffer = ChatMessage(role: 'assistant', content: chunk, ts: ts, isStreaming: true);
    } else {
      _streamBuffer = _streamBuffer!.appendChunk(chunk);
    }
    notifyListeners();
  }

  void _finalizeStream() {
    if (_streamBuffer == null) return;
    if (_activeChatAgent != null) {
      final msgs = _chats[_activeChatAgent!] ??= [];
      msgs.add(_streamBuffer!.finalized());
    }
    _streamBuffer = null;
    notifyListeners();
  }

  ChatMessage? _streamBuffer;
  String? _activeChatAgent;

  void setActiveChatAgent(String? agentName) {
    _activeChatAgent = agentName;
  }

  ChatMessage? get streamBuffer => _streamBuffer;

  void sendMessage(String content, {String? toAgent}) {
    if (_ws == null || content.trim().isEmpty) return;
    final msg = jsonEncode({'type': 'chat', 'content': content.trim(), 'to': toAgent});
    _ws!.sink.add(msg);

    final agent = toAgent ?? _activeChatAgent ?? 'main';
    final msgs = _chats[agent] ??= [];
    msgs.add(ChatMessage(
      role: 'user',
      content: content.trim(),
      ts: DateTime.now().millisecondsSinceEpoch / 1000,
    ));
    notifyListeners();
  }

  List<ChatMessage> messagesFor(String agentName) =>
      List.unmodifiable(_chats[agentName] ?? []);

  Future<void> loadChatHistory(String agentName) async {
    if (_baseUrl.isEmpty) return;
    try {
      final uri = Uri.parse('$_baseUrl/api/chats?agent=${Uri.encodeComponent(agentName)}&limit=100');
      final res = await _http.get(uri).timeout(const Duration(seconds: 8));
      if (res.statusCode == 200) {
        final rows = jsonDecode(res.body) as List;
        final msgs = rows.map((r) => ChatMessage.fromJson(r as Map<String, dynamic>)).toList();
        _chats[agentName] = msgs;
        notifyListeners();
      }
    } catch (_) {}
  }

  void _onWsError(Object err) {
    _setConnState(WsState.error);
    _errorMessage = err.toString();
    _scheduleReconnect();
  }

  void _onWsDone() {
    if (_connState == WsState.connected) {
      _setConnState(WsState.disconnected);
      _scheduleReconnect();
    }
  }

  void _scheduleReconnect() {
    _reconnectTimer?.cancel();
    _reconnectTimer = Timer(const Duration(seconds: 4), connect);
  }

  Future<void> _disconnect() async {
    _reconnectTimer?.cancel();
    await _wsSub?.cancel();
    await _ws?.sink.close();
    _ws = null;
    _wsSub = null;
  }

  void _setConnState(WsState s) {
    _connState = s;
    notifyListeners();
  }

  @override
  void dispose() {
    unawaited(_disconnect());
    _http.close();
    super.dispose();
  }
}
