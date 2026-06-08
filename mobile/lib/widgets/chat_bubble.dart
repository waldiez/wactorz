import 'package:flutter/material.dart';
import 'package:flutter_markdown/flutter_markdown.dart';
import '../models/chat_message.dart';
import '../theme.dart';

class ChatBubble extends StatelessWidget {
  final ChatMessage message;
  const ChatBubble({super.key, required this.message});

  @override
  Widget build(BuildContext context) {
    final isUser = message.isUser;
    return Align(
      alignment: isUser ? Alignment.centerRight : Alignment.centerLeft,
      child: ConstrainedBox(
        constraints: BoxConstraints(
          maxWidth: MediaQuery.of(context).size.width * 0.82,
        ),
        child: Container(
          margin: const EdgeInsets.symmetric(vertical: 4, horizontal: 16),
          decoration: BoxDecoration(
            color: isUser
                ? kPrimary.withValues(alpha: 0.18)
                : kSurfaceHigh,
            borderRadius: BorderRadius.only(
              topLeft: const Radius.circular(16),
              topRight: const Radius.circular(16),
              bottomLeft: Radius.circular(isUser ? 16 : 4),
              bottomRight: Radius.circular(isUser ? 4 : 16),
            ),
            border: Border.all(
              color: isUser
                  ? kPrimary.withValues(alpha: 0.3)
                  : kBorder,
            ),
          ),
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                if (isUser)
                  Text(
                    message.content,
                    style: const TextStyle(
                      color: kTextPrimary,
                      fontSize: 14,
                      height: 1.5,
                    ),
                  )
                else
                  MarkdownBody(
                    data: message.content.isEmpty && message.isStreaming
                        ? '▋'
                        : message.content + (message.isStreaming ? ' ▋' : ''),
                    styleSheet: MarkdownStyleSheet(
                      p: const TextStyle(
                          color: kTextPrimary, fontSize: 14, height: 1.6),
                      code: TextStyle(
                        color: kCyan,
                        backgroundColor: kBg,
                        fontSize: 12,
                        fontFamily: 'monospace',
                      ),
                      codeblockDecoration: BoxDecoration(
                        color: kBg,
                        borderRadius: BorderRadius.circular(8),
                        border: Border.all(color: kBorder),
                      ),
                      blockquote: const TextStyle(
                          color: kTextSecondary, fontStyle: FontStyle.italic),
                      blockquoteDecoration: const BoxDecoration(
                        border: Border(
                            left: BorderSide(color: kDim, width: 3)),
                      ),
                      h1: const TextStyle(
                          color: kTextPrimary,
                          fontSize: 16,
                          fontWeight: FontWeight.w700),
                      h2: const TextStyle(
                          color: kTextPrimary,
                          fontSize: 15,
                          fontWeight: FontWeight.w600),
                      strong: const TextStyle(
                          color: kTextPrimary, fontWeight: FontWeight.w700),
                      em: const TextStyle(
                          color: kTextSecondary, fontStyle: FontStyle.italic),
                      listBullet:
                          const TextStyle(color: kPrimary, fontSize: 14),
                    ),
                  ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class TypingIndicator extends StatefulWidget {
  const TypingIndicator({super.key});

  @override
  State<TypingIndicator> createState() => _TypingIndicatorState();
}

class _TypingIndicatorState extends State<TypingIndicator>
    with SingleTickerProviderStateMixin {
  late AnimationController _ctrl;

  @override
  void initState() {
    super.initState();
    _ctrl = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 900),
    )..repeat();
  }

  @override
  void dispose() {
    _ctrl.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Align(
      alignment: Alignment.centerLeft,
      child: Container(
        margin: const EdgeInsets.symmetric(vertical: 4, horizontal: 16),
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
        decoration: BoxDecoration(
          color: kSurfaceHigh,
          borderRadius: const BorderRadius.only(
            topLeft: Radius.circular(16),
            topRight: Radius.circular(16),
            bottomRight: Radius.circular(16),
            bottomLeft: Radius.circular(4),
          ),
          border: Border.all(color: kBorder),
        ),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: List.generate(3, (i) {
            return AnimatedBuilder(
              animation: _ctrl,
              builder: (context, child) {
                final offset = ((_ctrl.value * 3) - i).clamp(0.0, 1.0);
                final bounce = Curves.easeInOut.transform(
                  offset < 0.5 ? offset * 2 : (1.0 - offset) * 2,
                );
                return Container(
                  margin: const EdgeInsets.symmetric(horizontal: 2),
                  width: 6,
                  height: 6,
                  transform: Matrix4.translationValues(0, -4 * bounce, 0),
                  decoration: BoxDecoration(
                    shape: BoxShape.circle,
                    color: kPrimary.withValues(alpha: 0.5 + 0.5 * bounce),
                  ),
                );
              },
            );
          }),
        ),
      ),
    );
  }
}
