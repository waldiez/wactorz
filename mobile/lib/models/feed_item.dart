class FeedItem {
  final String type;
  final String label;
  final String agentName;
  final double timestamp;

  const FeedItem({
    required this.type,
    required this.label,
    required this.agentName,
    required this.timestamp,
  });

  factory FeedItem.fromJson(Map<String, dynamic> j) => FeedItem(
        type: j['type'] as String? ?? 'chat',
        label: j['label'] as String? ?? '',
        agentName: j['agentName'] as String? ?? '',
        timestamp: (j['timestamp'] as num?)?.toDouble() ?? 0.0,
      );

  DateTime get dateTime =>
      DateTime.fromMillisecondsSinceEpoch((timestamp * 1000).toInt());
}
