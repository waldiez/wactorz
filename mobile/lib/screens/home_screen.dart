import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import '../services/wactorz_client.dart';
import '../theme.dart';
import 'agents_tab.dart';
import 'feed_tab.dart';
import 'setup_screen.dart';

class HomeScreen extends StatefulWidget {
  const HomeScreen({super.key});

  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> {
  int _tab = 0;

  @override
  Widget build(BuildContext context) {
    return Consumer<WactorzClient>(
      builder: (context, client, _) {
        return Scaffold(
          appBar: AppBar(
            title: Row(
              children: [
                const Text('Wactorz'),
                const SizedBox(width: 10),
                _ConnectionBadge(state: client.connState),
              ],
            ),
            actions: [
              IconButton(
                icon: const Icon(Icons.settings_outlined),
                onPressed: () => Navigator.push(
                  context,
                  MaterialPageRoute(builder: (_) => const SetupScreen()),
                ),
              ),
            ],
          ),
          body: IndexedStack(
            index: _tab,
            children: const [AgentsTab(), FeedTab()],
          ),
          bottomNavigationBar: NavigationBar(
            selectedIndex: _tab,
            onDestinationSelected: (i) => setState(() => _tab = i),
            destinations: [
              NavigationDestination(
                icon: const Icon(Icons.memory_outlined),
                selectedIcon: const Icon(Icons.memory),
                label: 'Agents${client.agents.isNotEmpty ? ' (${client.agents.length})' : ''}',
              ),
              const NavigationDestination(
                icon: Icon(Icons.stream_outlined),
                selectedIcon: Icon(Icons.stream),
                label: 'Feed',
              ),
            ],
          ),
        );
      },
    );
  }
}

class _ConnectionBadge extends StatelessWidget {
  final WsState state;
  const _ConnectionBadge({required this.state});

  @override
  Widget build(BuildContext context) {
    final (color, label) = switch (state) {
      WsState.connected => (kGreen, 'live'),
      WsState.connecting => (kAmber, 'connecting'),
      WsState.error => (kRed, 'error'),
      _ => (kDim, 'offline'),
    };

    return AnimatedContainer(
      duration: const Duration(milliseconds: 300),
      padding: const EdgeInsets.symmetric(horizontal: 7, vertical: 3),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.12),
        borderRadius: BorderRadius.circular(6),
        border: Border.all(color: color.withValues(alpha: 0.3)),
      ),
      child: Text(
        label,
        style: TextStyle(
          color: color,
          fontSize: 10,
          fontWeight: FontWeight.w600,
          letterSpacing: 0.3,
        ),
      ),
    );
  }
}
