import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import '../services/wactorz_client.dart';
import '../theme.dart';
import '../widgets/feed_tile.dart';

class FeedTab extends StatefulWidget {
  const FeedTab({super.key});

  @override
  State<FeedTab> createState() => _FeedTabState();
}

class _FeedTabState extends State<FeedTab> {
  final _scroll = ScrollController();
  bool _paused = false;

  @override
  void dispose() {
    _scroll.dispose();
    super.dispose();
  }

  void _scrollBottom() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_scroll.hasClients && !_paused) {
        _scroll.animateTo(
          _scroll.position.maxScrollExtent,
          duration: const Duration(milliseconds: 200),
          curve: Curves.easeOut,
        );
      }
    });
  }

  @override
  Widget build(BuildContext context) {
    return Consumer<WactorzClient>(
      builder: (context, client, _) {
        final feed = client.feed;
        if (feed.isNotEmpty) _scrollBottom();

        return NotificationListener<ScrollNotification>(
          onNotification: (n) {
            if (n is ScrollStartNotification) setState(() => _paused = true);
            if (n is ScrollEndNotification) {
              final atBottom = _scroll.hasClients &&
                  _scroll.position.pixels >=
                      _scroll.position.maxScrollExtent - 40;
              if (atBottom) setState(() => _paused = false);
            }
            return false;
          },
          child: feed.isEmpty
              ? const Center(
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      Icon(Icons.stream, color: kDim, size: 40),
                      SizedBox(height: 12),
                      Text(
                        'No events yet',
                        style: TextStyle(color: kDim, fontSize: 14),
                      ),
                    ],
                  ),
                )
              : Stack(
                  children: [
                    ListView.builder(
                      controller: _scroll,
                      padding: const EdgeInsets.only(top: 12, bottom: 24),
                      itemCount: feed.length,
                      itemBuilder: (_, i) => FeedTile(item: feed[i]),
                    ),
                    if (_paused)
                      Positioned(
                        bottom: 16,
                        right: 16,
                        child: GestureDetector(
                          onTap: () {
                            setState(() => _paused = false);
                            _scrollBottom();
                          },
                          child: Container(
                            padding: const EdgeInsets.symmetric(
                                horizontal: 12, vertical: 6),
                            decoration: BoxDecoration(
                              color: kSurface,
                              borderRadius: BorderRadius.circular(20),
                              border: Border.all(color: kBorder),
                            ),
                            child: const Row(
                              mainAxisSize: MainAxisSize.min,
                              children: [
                                Icon(Icons.arrow_downward_rounded,
                                    size: 14, color: kPrimary),
                                SizedBox(width: 4),
                                Text('Live',
                                    style: TextStyle(
                                        color: kPrimary, fontSize: 12)),
                              ],
                            ),
                          ),
                        ),
                      ),
                  ],
                ),
        );
      },
    );
  }
}
