class ChatMessage {
  final String role;
  final String content;
  final double ts;
  final bool isStreaming;

  const ChatMessage({
    required this.role,
    required this.content,
    required this.ts,
    this.isStreaming = false,
  });

  factory ChatMessage.fromJson(Map<String, dynamic> j) => ChatMessage(
        role: j['role'] as String? ?? '',
        content: j['content'] as String? ?? '',
        ts: (j['ts'] as num?)?.toDouble() ?? 0.0,
      );

  ChatMessage appendChunk(String chunk) => ChatMessage(
        role: role,
        content: content + chunk,
        ts: ts,
        isStreaming: true,
      );

  ChatMessage finalized() => ChatMessage(
        role: role,
        content: content,
        ts: ts,
        isStreaming: false,
      );

  bool get isUser => role == 'user';
  bool get isAssistant => role == 'assistant';

  DateTime get dateTime =>
      DateTime.fromMillisecondsSinceEpoch((ts * 1000).toInt());
}
