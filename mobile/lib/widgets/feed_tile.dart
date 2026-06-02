import 'package:flutter/material.dart';
import 'package:timeago/timeago.dart' as timeago;
import '../models/feed_item.dart';
import '../theme.dart';

class FeedTile extends StatelessWidget {
  final FeedItem item;
  const FeedTile({super.key, required this.item});

  Color get _color => switch (item.type) {
        'spawn' => kGreen,
        'heartbeat' || 'health' => kPrimary,
        'chat' => kCyan,
        'alert-error' => kRed,
        'alert-warning' => kAmber,
        'stopped' => kDim,
        'qa-flag' => kPurple,
        _ => kTextSecondary,
      };

  String get _icon => switch (item.type) {
        'spawn' => '⊕',
        'heartbeat' || 'health' => '♥',
        'chat' => '◈',
        'alert-error' => '⚠',
        'alert-warning' => '⚡',
        'stopped' => '◻',
        'qa-flag' => '⚑',
        _ => '·',
      };

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 5),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Padding(
            padding: const EdgeInsets.only(top: 1),
            child: Text(
              _icon,
              style: TextStyle(color: _color, fontSize: 12),
            ),
          ),
          const SizedBox(width: 10),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    Text(
                      item.agentName,
                      style: TextStyle(
                        color: _color,
                        fontSize: 11,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                    const Spacer(),
                    Text(
                      timeago.format(item.dateTime, allowFromNow: true),
                      style: const TextStyle(color: kDim, fontSize: 10),
                    ),
                  ],
                ),
                const SizedBox(height: 2),
                Text(
                  item.label,
                  style: const TextStyle(
                    color: kTextSecondary,
                    fontSize: 12,
                  ),
                  maxLines: 3,
                  overflow: TextOverflow.ellipsis,
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}
