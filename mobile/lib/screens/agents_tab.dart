import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import '../services/wactorz_client.dart';
import '../theme.dart';
import '../widgets/agent_card.dart';
import 'chat_screen.dart';

class AgentsTab extends StatelessWidget {
  const AgentsTab({super.key});

  @override
  Widget build(BuildContext context) {
    return Consumer<WactorzClient>(
      builder: (context, client, _) {
        final agents = client.agents;

        if (client.connState == WsState.disconnected ||
            client.connState == WsState.error) {
          return _DisconnectedState(error: client.errorMessage);
        }

        if (client.connState == WsState.connecting && agents.isEmpty) {
          return const Center(
            child: CircularProgressIndicator(color: kPrimary, strokeWidth: 2),
          );
        }

        if (agents.isEmpty) {
          return const Center(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                Icon(Icons.memory_outlined, color: kDim, size: 40),
                SizedBox(height: 12),
                Text('No agents running', style: TextStyle(color: kDim, fontSize: 14)),
              ],
            ),
          );
        }

        return CustomScrollView(
          slivers: [
            SliverPadding(
              padding: const EdgeInsets.fromLTRB(16, 16, 16, 4),
              sliver: SliverToBoxAdapter(child: _StatsBar(client: client)),
            ),
            SliverPadding(
              padding: const EdgeInsets.fromLTRB(16, 12, 16, 24),
              sliver: SliverGrid(
                gridDelegate: const SliverGridDelegateWithMaxCrossAxisExtent(
                  maxCrossAxisExtent: 200,
                  mainAxisExtent: 160,
                  mainAxisSpacing: 12,
                  crossAxisSpacing: 12,
                ),
                delegate: SliverChildBuilderDelegate(
                  (context, i) {
                    final agent = agents[i];
                    return AgentCard(
                      agent: agent,
                      onTap: () => Navigator.push(
                        context,
                        MaterialPageRoute(
                          builder: (_) => ChatScreen(agent: agent),
                        ),
                      ),
                    );
                  },
                  childCount: agents.length,
                ),
              ),
            ),
          ],
        );
      },
    );
  }
}

class _StatsBar extends StatelessWidget {
  final WactorzClient client;
  const _StatsBar({required this.client});

  @override
  Widget build(BuildContext context) {
    final running = client.agents.where((a) => a.isRunning).length;
    return Row(
      children: [
        _StatPill(
          label: '$running running',
          color: running > 0 ? kGreen : kDim,
        ),
        const SizedBox(width: 8),
        _StatPill(
          label: '${client.agents.length} total',
          color: kPrimary,
        ),
        const Spacer(),
        _StatPill(
          label: '\$${client.totalCostUsd.toStringAsFixed(4)}',
          color: kAmber,
        ),
      ],
    );
  }
}

class _StatPill extends StatelessWidget {
  final String label;
  final Color color;
  const _StatPill({required this.label, required this.color});

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.1),
        borderRadius: BorderRadius.circular(20),
        border: Border.all(color: color.withValues(alpha: 0.25)),
      ),
      child: Text(
        label,
        style: TextStyle(
          color: color,
          fontSize: 11,
          fontWeight: FontWeight.w600,
        ),
      ),
    );
  }
}

class _DisconnectedState extends StatelessWidget {
  final String? error;
  const _DisconnectedState({this.error});

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(32),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Icon(Icons.wifi_off_rounded, color: kDim, size: 40),
            const SizedBox(height: 12),
            const Text(
              'Not connected',
              style: TextStyle(
                  color: kTextPrimary,
                  fontSize: 16,
                  fontWeight: FontWeight.w600),
            ),
            if (error != null) ...[
              const SizedBox(height: 6),
              Text(
                error!,
                style: const TextStyle(color: kDim, fontSize: 12),
                textAlign: TextAlign.center,
              ),
            ],
            const SizedBox(height: 20),
            OutlinedButton(
              onPressed: () => context.read<WactorzClient>().connect(),
              style: OutlinedButton.styleFrom(
                foregroundColor: kPrimary,
                side: const BorderSide(color: kPrimary),
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(10),
                ),
              ),
              child: const Text('Retry'),
            ),
          ],
        ),
      ),
    );
  }
}
