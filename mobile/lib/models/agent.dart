class Agent {
  final String id;
  final String name;
  final String state;
  final bool protected;
  final double? cpu;
  final double? mem;
  final String? task;
  final int messagesProcessed;
  final double costUsd;

  const Agent({
    required this.id,
    required this.name,
    required this.state,
    required this.protected,
    this.cpu,
    this.mem,
    this.task,
    required this.messagesProcessed,
    required this.costUsd,
  });

  // Accepts both camelCase (REST/actor_payload) and snake_case (raw WS snapshot).
  factory Agent.fromJson(Map<String, dynamic> j) => Agent(
        id: j['id'] as String? ?? j['agent_id'] as String? ?? '',
        name: j['name'] as String? ?? '',
        state: j['state'] as String? ?? 'unknown',
        protected: j['protected'] as bool? ?? false,
        cpu: (j['cpu'] as num?)?.toDouble(),
        mem: (j['mem'] as num?)?.toDouble(),
        task: j['task'] as String?,
        messagesProcessed: (j['messagesProcessed'] as num?)?.toInt() ??
            (j['messages_processed'] as num?)?.toInt() ??
            0,
        costUsd: (j['costUsd'] as num?)?.toDouble() ??
            (j['cost_usd'] as num?)?.toDouble() ??
            0.0,
      );

  Agent copyWith(Map<String, dynamic> j) => Agent(
        id: j['id'] as String? ?? j['agent_id'] as String? ?? id,
        name: j['name'] as String? ?? name,
        state: j['state'] as String? ?? state,
        protected: j['protected'] as bool? ?? protected,
        cpu: (j['cpu'] as num?)?.toDouble() ?? cpu,
        mem: (j['mem'] as num?)?.toDouble() ?? mem,
        task: j['task'] as String? ?? task,
        messagesProcessed: (j['messagesProcessed'] as num?)?.toInt() ??
            (j['messages_processed'] as num?)?.toInt() ??
            messagesProcessed,
        costUsd: (j['costUsd'] as num?)?.toDouble() ??
            (j['cost_usd'] as num?)?.toDouble() ??
            costUsd,
      );

  bool get isRunning => state == 'running';
  bool get isFailed  => state == 'failed';
  bool get isPaused  => state == 'paused';
  bool get isStopped => state == 'stopped' || state == 'failed';
}
