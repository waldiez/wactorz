import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import 'services/wactorz_client.dart';
import 'theme.dart';
import 'screens/home_screen.dart';
import 'screens/setup_screen.dart';

void main() {
  runApp(
    MultiProvider(
      providers: [
        ChangeNotifierProvider(create: (_) => WactorzClient()),
      ],
      child: const WactorzApp(),
    ),
  );
}

class WactorzApp extends StatelessWidget {
  const WactorzApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'wactorz',
      theme: buildTheme(),
      debugShowCheckedModeBanner: false,
      home: const _Gate(),
    );
  }
}

class _Gate extends StatefulWidget {
  const _Gate();

  @override
  State<_Gate> createState() => _GateState();
}

class _GateState extends State<_Gate> {
  bool _ready = false;

  @override
  void initState() {
    super.initState();
    context.read<WactorzClient>().init().then((_) {
      if (mounted) setState(() => _ready = true);
    });
  }

  @override
  Widget build(BuildContext context) {
    if (!_ready) {
      return const Scaffold(
        body: Center(child: CircularProgressIndicator(color: kPrimary)),
      );
    }
    final baseUrl = context.select<WactorzClient, String>((c) => c.baseUrl);
    return baseUrl.isEmpty ? const SetupScreen() : const HomeScreen();
  }
}
