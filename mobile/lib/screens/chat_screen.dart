import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import '../models/agent.dart';
import '../services/wactorz_client.dart';
import '../theme.dart';
import '../widgets/chat_bubble.dart';
import '../widgets/status_dot.dart';

class ChatScreen extends StatefulWidget {
  final Agent agent;
  const ChatScreen({super.key, required this.agent});

  @override
  State<ChatScreen> createState() => _ChatScreenState();
}

class _ChatScreenState extends State<ChatScreen> {
  final _input = TextEditingController();
  final _scroll = ScrollController();
  bool _sending = false;

  @override
  void initState() {
    super.initState();
    final client = context.read<WactorzClient>();
    client.setActiveChatAgent(widget.agent.name);
    client.loadChatHistory(widget.agent.name).then((_) => _scrollBottom());
  }

  @override
  void dispose() {
    _input.dispose();
    _scroll.dispose();
    super.dispose();
  }

  void _scrollBottom() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_scroll.hasClients) {
        _scroll.animateTo(
          _scroll.position.maxScrollExtent,
          duration: const Duration(milliseconds: 250),
          curve: Curves.easeOut,
        );
      }
    });
  }

  void _send() {
    final text = _input.text.trim();
    if (text.isEmpty || _sending) return;
    _input.clear();
    setState(() => _sending = true);
    context.read<WactorzClient>().sendMessage(text, toAgent: widget.agent.name);
    Future.delayed(const Duration(milliseconds: 300), () {
      if (mounted) setState(() => _sending = false);
    });
    _scrollBottom();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Row(
          children: [
            StatusDot(state: widget.agent.state, size: 8),
            const SizedBox(width: 8),
            Text(widget.agent.name),
          ],
        ),
        actions: [
          Padding(
            padding: const EdgeInsets.only(right: 16),
            child: Center(
              child: _AgentStats(agent: widget.agent),
            ),
          ),
        ],
      ),
      body: Consumer<WactorzClient>(
        builder: (context, client, _) {
          final messages = client.messagesFor(widget.agent.name);
          final streaming = client.streamBuffer;
          final hasStream = streaming != null && client.streamBuffer != null;

          // Auto-scroll on new messages
          if (messages.isNotEmpty || hasStream) {
            _scrollBottom();
          }

          return Column(
            children: [
              Expanded(
                child: messages.isEmpty && !hasStream
                    ? _EmptyState(agentName: widget.agent.name)
                    : ListView.builder(
                        controller: _scroll,
                        padding: const EdgeInsets.only(top: 12, bottom: 8),
                        itemCount: messages.length + (hasStream ? 1 : 0),
                        itemBuilder: (_, i) {
                          if (i < messages.length) {
                            return ChatBubble(message: messages[i]);
                          }
                          return ChatBubble(message: streaming!);
                        },
                      ),
              ),
              _InputBar(
                controller: _input,
                onSend: _send,
                enabled: client.connState == WsState.connected,
              ),
            ],
          );
        },
      ),
    );
  }
}

class _AgentStats extends StatelessWidget {
  final Agent agent;
  const _AgentStats({required this.agent});

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        const Icon(Icons.chat_bubble_outline, size: 12, color: kDim),
        const SizedBox(width: 4),
        Text(
          '${agent.messagesProcessed}',
          style: const TextStyle(color: kDim, fontSize: 11),
        ),
        if (agent.costUsd > 0) ...[
          const SizedBox(width: 10),
          const Icon(Icons.monetization_on_outlined, size: 12, color: kDim),
          const SizedBox(width: 4),
          Text(
            '\$${agent.costUsd.toStringAsFixed(4)}',
            style: const TextStyle(color: kDim, fontSize: 11),
          ),
        ],
      ],
    );
  }
}

class _EmptyState extends StatelessWidget {
  final String agentName;
  const _EmptyState({required this.agentName});

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Container(
            width: 56,
            height: 56,
            decoration: BoxDecoration(
              shape: BoxShape.circle,
              color: kPrimary.withValues(alpha: 0.08),
              border: Border.all(color: kPrimary.withValues(alpha: 0.2)),
            ),
            child: const Icon(Icons.chat_bubble_outline, color: kPrimary, size: 22),
          ),
          const SizedBox(height: 16),
          Text(
            'Chat with $agentName',
            style: const TextStyle(
              color: kTextPrimary,
              fontSize: 16,
              fontWeight: FontWeight.w600,
            ),
          ),
          const SizedBox(height: 6),
          const Text(
            'Send a message to get started',
            style: TextStyle(color: kTextSecondary, fontSize: 13),
          ),
        ],
      ),
    );
  }
}

class _InputBar extends StatelessWidget {
  final TextEditingController controller;
  final VoidCallback onSend;
  final bool enabled;

  const _InputBar({
    required this.controller,
    required this.onSend,
    required this.enabled,
  });

  @override
  Widget build(BuildContext context) {
    final bottom = MediaQuery.of(context).viewInsets.bottom;
    return Container(
      padding: EdgeInsets.fromLTRB(12, 10, 12, 12 + bottom),
      decoration: const BoxDecoration(
        color: kSurface,
        border: Border(top: BorderSide(color: kBorder)),
      ),
      child: Row(
        children: [
          Expanded(
            child: TextField(
              controller: controller,
              style: const TextStyle(color: kTextPrimary, fontSize: 14),
              maxLines: 4,
              minLines: 1,
              enabled: enabled,
              textInputAction: TextInputAction.send,
              onSubmitted: (_) => onSend(),
              decoration: InputDecoration(
                hintText: enabled ? 'Message…' : 'Connecting…',
                contentPadding: const EdgeInsets.symmetric(
                  horizontal: 16,
                  vertical: 12,
                ),
              ),
            ),
          ),
          const SizedBox(width: 8),
          _SendButton(onTap: enabled ? onSend : null),
        ],
      ),
    );
  }
}

class _SendButton extends StatelessWidget {
  final VoidCallback? onTap;
  const _SendButton({this.onTap});

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      child: AnimatedContainer(
        duration: const Duration(milliseconds: 150),
        width: 42,
        height: 42,
        decoration: BoxDecoration(
          shape: BoxShape.circle,
          color: onTap != null
              ? kPrimary.withValues(alpha: 0.15)
              : kSurfaceHigh,
          border: Border.all(
            color: onTap != null
                ? kPrimary.withValues(alpha: 0.4)
                : kBorder,
          ),
        ),
        child: Icon(
          Icons.arrow_upward_rounded,
          color: onTap != null ? kPrimary : kDim,
          size: 18,
        ),
      ),
    );
  }
}
