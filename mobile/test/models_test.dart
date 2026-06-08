import 'package:flutter_test/flutter_test.dart';
import 'package:wactorz/models/agent.dart';
import 'package:wactorz/models/chat_message.dart';
import 'package:wactorz/models/feed_item.dart';

void main() {
  // ── Agent ──────────────────────────────────────────────────────────────────

  group('Agent.fromJson', () {
    test('camelCase keys (REST / actor_payload format)', () {
      final a = Agent.fromJson({
        'id': 'abc123',
        'name': 'test-agent',
        'state': 'running',
        'protected': true,
        'cpu': 0.5,
        'mem': 100.0,
        'task': 'doing stuff',
        'messagesProcessed': 42,
        'costUsd': 0.0012,
      });
      expect(a.id, 'abc123');
      expect(a.name, 'test-agent');
      expect(a.state, 'running');
      expect(a.protected, true);
      expect(a.cpu, 0.5);
      expect(a.mem, 100.0);
      expect(a.task, 'doing stuff');
      expect(a.messagesProcessed, 42);
      expect(a.costUsd, 0.0012);
    });

    test('snake_case keys (raw WS snapshot format)', () {
      final a = Agent.fromJson({
        'agent_id': 'def456',
        'name': 'raw-agent',
        'state': 'stopped',
        'messages_processed': 10,
        'cost_usd': 0.001,
      });
      expect(a.id, 'def456');
      expect(a.name, 'raw-agent');
      expect(a.messagesProcessed, 10);
      expect(a.costUsd, 0.001);
    });

    test('missing fields use defaults', () {
      final a = Agent.fromJson({});
      expect(a.id, '');
      expect(a.name, '');
      expect(a.state, 'unknown');
      expect(a.protected, false);
      expect(a.cpu, null);
      expect(a.mem, null);
      expect(a.task, null);
      expect(a.messagesProcessed, 0);
      expect(a.costUsd, 0.0);
    });

    test('camelCase takes precedence over snake_case when both present', () {
      final a = Agent.fromJson({
        'id': 'camel',
        'agent_id': 'snake',
        'messagesProcessed': 5,
        'messages_processed': 3,
      });
      expect(a.id, 'camel');
      expect(a.messagesProcessed, 5);
    });
  });

  group('Agent state getters', () {
    test('isRunning', () {
      expect(Agent.fromJson({'state': 'running'}).isRunning, true);
      expect(Agent.fromJson({'state': 'stopped'}).isRunning, false);
      expect(Agent.fromJson({'state': 'failed'}).isRunning, false);
    });

    test('isFailed', () {
      expect(Agent.fromJson({'state': 'failed'}).isFailed, true);
      expect(Agent.fromJson({'state': 'stopped'}).isFailed, false);
    });

    test('isPaused', () {
      expect(Agent.fromJson({'state': 'paused'}).isPaused, true);
      expect(Agent.fromJson({'state': 'running'}).isPaused, false);
    });

    test('isStopped covers stopped and failed', () {
      expect(Agent.fromJson({'state': 'stopped'}).isStopped, true);
      expect(Agent.fromJson({'state': 'failed'}).isStopped, true);
      expect(Agent.fromJson({'state': 'running'}).isStopped, false);
    });
  });

  group('Agent.copyWith', () {
    test('updates provided fields, keeps others', () {
      final base = Agent.fromJson({
        'agent_id': 'x',
        'name': 'base',
        'state': 'running',
        'messages_processed': 1,
        'cost_usd': 0.1,
      });
      final updated = base.copyWith({
        'state': 'stopped',
        'messagesProcessed': 5,
      });
      expect(updated.state, 'stopped');
      expect(updated.messagesProcessed, 5);
      expect(updated.name, 'base');
      expect(updated.id, 'x');
      expect(updated.costUsd, 0.1);
    });

    test('handles snake_case in copyWith', () {
      final base = Agent.fromJson({'agent_id': 'y', 'name': 'b'});
      final updated = base.copyWith({'messages_processed': 99, 'cost_usd': 0.5});
      expect(updated.messagesProcessed, 99);
      expect(updated.costUsd, 0.5);
    });

    test('empty map keeps all fields', () {
      final base = Agent.fromJson({'agent_id': 'z', 'name': 'keep', 'state': 'running'});
      final same = base.copyWith({});
      expect(same.id, 'z');
      expect(same.name, 'keep');
      expect(same.state, 'running');
    });
  });

  // ── ChatMessage ────────────────────────────────────────────────────────────

  group('ChatMessage.fromJson', () {
    test('parses all fields', () {
      final m = ChatMessage.fromJson({
        'role': 'user',
        'content': 'hello',
        'ts': 1700000000.0,
      });
      expect(m.role, 'user');
      expect(m.content, 'hello');
      expect(m.ts, 1700000000.0);
      expect(m.isStreaming, false);
    });

    test('defaults for missing fields', () {
      final m = ChatMessage.fromJson({});
      expect(m.role, '');
      expect(m.content, '');
      expect(m.ts, 0.0);
    });
  });

  group('ChatMessage getters', () {
    test('isUser / isAssistant', () {
      const u = ChatMessage(role: 'user', content: '', ts: 0);
      const a = ChatMessage(role: 'assistant', content: '', ts: 0);
      expect(u.isUser, true);
      expect(u.isAssistant, false);
      expect(a.isAssistant, true);
      expect(a.isUser, false);
    });

    test('dateTime converts unix timestamp', () {
      const m = ChatMessage(role: 'user', content: '', ts: 1700000000.0);
      expect(m.dateTime.millisecondsSinceEpoch, 1700000000000);
    });
  });

  group('ChatMessage streaming', () {
    test('appendChunk concatenates and marks streaming', () {
      const m = ChatMessage(role: 'assistant', content: 'hello', ts: 0);
      final appended = m.appendChunk(' world');
      expect(appended.content, 'hello world');
      expect(appended.isStreaming, true);
      expect(appended.role, 'assistant');
      expect(appended.ts, 0);
    });

    test('appendChunk on empty content', () {
      const m = ChatMessage(role: 'assistant', content: '', ts: 1.0);
      final appended = m.appendChunk('first');
      expect(appended.content, 'first');
      expect(appended.isStreaming, true);
    });

    test('finalized clears isStreaming', () {
      const m = ChatMessage(role: 'assistant', content: 'done', ts: 0, isStreaming: true);
      final fin = m.finalized();
      expect(fin.isStreaming, false);
      expect(fin.content, 'done');
      expect(fin.role, 'assistant');
    });
  });

  // ── FeedItem ───────────────────────────────────────────────────────────────

  group('FeedItem.fromJson', () {
    test('parses all fields', () {
      final f = FeedItem.fromJson({
        'type': 'spawn',
        'label': 'agent started',
        'agentName': 'my-agent',
        'timestamp': 1700000000.0,
      });
      expect(f.type, 'spawn');
      expect(f.label, 'agent started');
      expect(f.agentName, 'my-agent');
      expect(f.timestamp, 1700000000.0);
    });

    test('defaults for missing fields', () {
      final f = FeedItem.fromJson({});
      expect(f.type, 'chat');
      expect(f.label, '');
      expect(f.agentName, '');
      expect(f.timestamp, 0.0);
    });

    test('dateTime converts unix timestamp', () {
      final f = FeedItem.fromJson({'timestamp': 1700000000.0});
      expect(f.dateTime.millisecondsSinceEpoch, 1700000000000);
    });
  });
}
