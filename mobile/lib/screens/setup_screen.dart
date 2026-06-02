import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import '../services/wactorz_client.dart';
import '../theme.dart';

class SetupScreen extends StatefulWidget {
  const SetupScreen({super.key});

  @override
  State<SetupScreen> createState() => _SetupScreenState();
}

class _SetupScreenState extends State<SetupScreen> {
  final _ctrl = TextEditingController(text: 'http://');
  bool _saving = false;

  @override
  void initState() {
    super.initState();
    final client = context.read<WactorzClient>();
    if (client.baseUrl.isNotEmpty) _ctrl.text = client.baseUrl;
  }

  @override
  void dispose() {
    _ctrl.dispose();
    super.dispose();
  }

  Future<void> _save() async {
    final url = _ctrl.text.trim();
    if (url.isEmpty || url == 'http://') return;
    setState(() => _saving = true);
    await context.read<WactorzClient>().setUrl(url);
    setState(() => _saving = false);
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: SafeArea(
        child: Center(
          child: SingleChildScrollView(
            padding: const EdgeInsets.all(32),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                // Logo / wordmark
                Container(
                  width: 64,
                  height: 64,
                  decoration: BoxDecoration(
                    shape: BoxShape.circle,
                    color: kPrimary.withValues(alpha: 0.1),
                    border: Border.all(color: kPrimary.withValues(alpha: 0.3)),
                  ),
                  child: const Icon(Icons.memory_outlined, color: kPrimary, size: 28),
                ),
                const SizedBox(height: 20),
                const Text(
                  'Wactorz',
                  style: TextStyle(
                    color: kTextPrimary,
                    fontSize: 26,
                    fontWeight: FontWeight.w700,
                    letterSpacing: -0.5,
                  ),
                ),
                const SizedBox(height: 6),
                const Text(
                  'Enter your server address to connect',
                  style: TextStyle(color: kTextSecondary, fontSize: 14),
                ),
                const SizedBox(height: 40),
                Container(
                  padding: const EdgeInsets.all(24),
                  decoration: BoxDecoration(
                    color: kSurface,
                    borderRadius: BorderRadius.circular(20),
                    border: Border.all(color: kBorder),
                  ),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      const Text(
                        'Server URL',
                        style: TextStyle(
                          color: kTextSecondary,
                          fontSize: 12,
                          fontWeight: FontWeight.w500,
                          letterSpacing: 0.5,
                        ),
                      ),
                      const SizedBox(height: 8),
                      TextField(
                        controller: _ctrl,
                        style: const TextStyle(color: kTextPrimary, fontSize: 14),
                        keyboardType: TextInputType.url,
                        autocorrect: false,
                        decoration: const InputDecoration(
                          hintText: 'http://192.168.1.x:8888',
                          prefixIcon: Icon(Icons.link, color: kDim, size: 18),
                        ),
                        onSubmitted: (_) => _save(),
                      ),
                      const SizedBox(height: 8),
                      const Text(
                        'Tip: use port 8888 (monitor UI) or 8000 (REST API)',
                        style: TextStyle(color: kDim, fontSize: 11),
                      ),
                      const SizedBox(height: 20),
                      FilledButton(
                        onPressed: _saving ? null : _save,
                        style: FilledButton.styleFrom(
                          backgroundColor: kPrimary,
                          foregroundColor: kBg,
                          padding: const EdgeInsets.symmetric(vertical: 14),
                          shape: RoundedRectangleBorder(
                            borderRadius: BorderRadius.circular(12),
                          ),
                        ),
                        child: _saving
                            ? const SizedBox(
                                width: 18,
                                height: 18,
                                child: CircularProgressIndicator(
                                  strokeWidth: 2,
                                  color: kBg,
                                ),
                              )
                            : const Text(
                                'Connect',
                                style: TextStyle(
                                  fontWeight: FontWeight.w600,
                                  fontSize: 15,
                                ),
                              ),
                      ),
                    ],
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
