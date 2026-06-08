import 'package:audioplayers/audioplayers.dart';
import 'package:flutter/foundation.dart';

class TtsService extends ChangeNotifier {
  final _player = AudioPlayer();
  bool _enabled = false;
  bool _playing = false;
  String _baseUrl = '';

  bool get enabled => _enabled;
  bool get playing => _playing;

  void setBaseUrl(String url) => _baseUrl = url;

  void toggle() {
    _enabled = !_enabled;
    if (!_enabled) _player.stop();
    notifyListeners();
  }

  Future<void> speak(String text) async {
    if (!_enabled || _baseUrl.isEmpty || text.trim().isEmpty) return;

    // Strip markdown, cap length — mirrors the backend logic
    final clean = text
        .replaceAll(RegExp(r'```[\s\S]*?```'), 'code block')
        .replaceAll(RegExp(r'[*_`#>]'), '')
        .trim();
    if (clean.isEmpty) return;

    final capped = clean.length > 300 ? '${clean.substring(0, 300)}…' : clean;
    final uri = '$_baseUrl/api/tts?text=${Uri.encodeComponent(capped)}';

    try {
      _playing = true;
      notifyListeners();
      await _player.play(UrlSource(uri));
    } catch (_) {
      // TTS not installed on server or network error — silent fail
    } finally {
      _playing = false;
      notifyListeners();
    }
  }

  void stop() {
    _player.stop();
    _playing = false;
    notifyListeners();
  }

  @override
  void dispose() {
    _player.dispose();
    super.dispose();
  }
}
