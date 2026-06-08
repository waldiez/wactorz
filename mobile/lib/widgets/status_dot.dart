import 'package:flutter/material.dart';
import '../theme.dart';

class StatusDot extends StatefulWidget {
  final String state;
  final double size;
  const StatusDot({super.key, required this.state, this.size = 8});

  @override
  State<StatusDot> createState() => _StatusDotState();
}

class _StatusDotState extends State<StatusDot>
    with SingleTickerProviderStateMixin {
  late AnimationController _ctrl;
  late Animation<double> _anim;

  @override
  void initState() {
    super.initState();
    _ctrl = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 1200),
    )..repeat(reverse: true);
    _anim = Tween<double>(begin: 0.4, end: 1.0).animate(
      CurvedAnimation(parent: _ctrl, curve: Curves.easeInOut),
    );
  }

  @override
  void dispose() {
    _ctrl.dispose();
    super.dispose();
  }

  Color get _color => switch (widget.state) {
        'running' => kGreen,
        'paused'  => kAmber,
        'failed'  => kRed,
        _         => kMuted,
      };

  @override
  Widget build(BuildContext context) {
    final color = _color;
    final s = widget.size;
    if (widget.state != 'running') {
      return Container(
        width: s,
        height: s,
        decoration: BoxDecoration(color: color, shape: BoxShape.circle),
      );
    }
    return AnimatedBuilder(
      animation: _anim,
      builder: (ctx, child) => Container(
        width: s,
        height: s,
        decoration: BoxDecoration(
          color: color.withAlpha((_anim.value * 255).toInt()),
          shape: BoxShape.circle,
          boxShadow: [
            BoxShadow(
              color: color.withAlpha((_anim.value * 120).toInt()),
              blurRadius: s * 1.5,
              spreadRadius: s * 0.3,
            ),
          ],
        ),
      ),
    );
  }
}
