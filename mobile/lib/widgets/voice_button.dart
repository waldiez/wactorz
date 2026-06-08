import 'dart:async';
import 'package:flutter/material.dart';
import 'package:speech_to_text/speech_to_text.dart';
import '../theme.dart';

class VoiceButton extends StatefulWidget {
  final void Function(String text) onResult;
  const VoiceButton({super.key, required this.onResult});

  @override
  State<VoiceButton> createState() => _VoiceButtonState();
}

class _VoiceButtonState extends State<VoiceButton>
    with TickerProviderStateMixin {
  final _stt = SpeechToText();
  bool _available = false;
  bool _listening = false;
  String _interim = '';

  late final AnimationController _ringCtrl;
  late final AnimationController _pulseCtrl;

  @override
  void initState() {
    super.initState();
    _ringCtrl = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 1400),
    );
    _pulseCtrl = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 700),
    );

    _stt.initialize(onStatus: _onStatus, onError: _onError).then((ok) {
      if (mounted) setState(() => _available = ok);
    });
  }

  @override
  void dispose() {
    _ringCtrl.dispose();
    _pulseCtrl.dispose();
    _stt.stop();
    super.dispose();
  }

  void _onStatus(String status) {
    if (!mounted) return;
    final nowListening = status == 'listening';
    setState(() => _listening = nowListening);
    if (nowListening) {
      _ringCtrl.repeat();
      _pulseCtrl.repeat(reverse: true);
    } else {
      _ringCtrl.stop();
      _ringCtrl.reset();
      _pulseCtrl.stop();
      _pulseCtrl.reset();
    }
  }

  void _onError(dynamic e) {
    if (!mounted) return;
    setState(() { _listening = false; _interim = ''; });
    _ringCtrl.stop();
    _ringCtrl.reset();
    _pulseCtrl.stop();
    _pulseCtrl.reset();
  }

  Future<void> _startListening() async {
    if (!_available || _listening) return;
    setState(() => _interim = '');
    await _stt.listen(
      onResult: (r) {
        if (!mounted) return;
        setState(() => _interim = r.recognizedWords);
        if (r.finalResult && r.recognizedWords.isNotEmpty) {
          widget.onResult(r.recognizedWords);
          setState(() => _interim = '');
        }
      },
      listenFor: const Duration(seconds: 30),
      pauseFor: const Duration(seconds: 3),
      listenOptions: SpeechListenOptions(
        partialResults: true,
        cancelOnError: true,
      ),
    );
  }

  Future<void> _stopListening() async {
    if (!_listening) return;
    await _stt.stop();
  }

  @override
  Widget build(BuildContext context) {
    if (!_available) return const SizedBox.shrink();

    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        if (_interim.isNotEmpty)
          Padding(
            padding: const EdgeInsets.only(bottom: 12),
            child: Container(
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
              decoration: BoxDecoration(
                color: kCard,
                borderRadius: BorderRadius.circular(20),
                border: Border.all(color: kBorder),
              ),
              child: Text(
                _interim,
                style: const TextStyle(color: kText, fontSize: 14),
                textAlign: TextAlign.center,
              ),
            ),
          ),
        GestureDetector(
          onTap: () {
            if (_listening) {
              _stopListening();
            } else {
              _startListening();
            }
          },
          child: _AnimatedMicButton(
            listening: _listening,
            ringCtrl: _ringCtrl,
            pulseCtrl: _pulseCtrl,
          ),
        ),
        const SizedBox(height: 6),
        Text(
          _listening ? 'Listening… tap to stop' : 'Tap to speak',
          style: TextStyle(
            fontSize: 11,
            color: _listening ? kGreen : kMuted,
            fontWeight: _listening ? FontWeight.w600 : FontWeight.normal,
          ),
        ),
      ],
    );
  }
}

class _AnimatedMicButton extends StatelessWidget {
  final bool listening;
  final AnimationController ringCtrl;
  final AnimationController pulseCtrl;

  const _AnimatedMicButton({
    required this.listening,
    required this.ringCtrl,
    required this.pulseCtrl,
  });

  @override
  Widget build(BuildContext context) {
    return AnimatedBuilder(
      animation: Listenable.merge([ringCtrl, pulseCtrl]),
      builder: (ctx, child) {
        final color = listening ? kGreen : kPrimary;
        final scale = listening ? (1.0 + pulseCtrl.value * 0.08) : 1.0;

        return SizedBox(
          width: 100,
          height: 100,
          child: Stack(
            alignment: Alignment.center,
            children: [
              // Expanding rings when listening
              if (listening) ...[
                _Ring(progress: (ringCtrl.value + 0.0) % 1.0, color: color, maxRadius: 60),
                _Ring(progress: (ringCtrl.value + 0.4) % 1.0, color: color, maxRadius: 60),
                _Ring(progress: (ringCtrl.value + 0.7) % 1.0, color: color, maxRadius: 60),
              ],
              // Main button
              Transform.scale(
                scale: scale,
                child: Container(
                  width: 72,
                  height: 72,
                  decoration: BoxDecoration(
                    shape: BoxShape.circle,
                    color: listening
                        ? kGreen.withAlpha(30)
                        : kPrimary.withAlpha(20),
                    border: Border.all(
                      color: color,
                      width: listening ? 2.5 : 1.5,
                    ),
                    boxShadow: [
                      BoxShadow(
                        color: color.withAlpha(listening ? 80 : 40),
                        blurRadius: listening ? 20 : 8,
                        spreadRadius: listening ? 4 : 0,
                      ),
                    ],
                  ),
                  child: Icon(
                    listening ? Icons.mic : Icons.mic_none_outlined,
                    color: color,
                    size: 32,
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

class _Ring extends StatelessWidget {
  final double progress; // 0..1
  final Color color;
  final double maxRadius;
  const _Ring({required this.progress, required this.color, required this.maxRadius});

  @override
  Widget build(BuildContext context) {
    final radius = maxRadius * progress;
    final opacity = (1.0 - progress).clamp(0.0, 1.0);
    return Container(
      width: radius * 2,
      height: radius * 2,
      decoration: BoxDecoration(
        shape: BoxShape.circle,
        border: Border.all(
          color: color.withAlpha((opacity * 120).toInt()),
          width: 1.5,
        ),
      ),
    );
  }
}
