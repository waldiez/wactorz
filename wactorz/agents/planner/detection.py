"""Does a request describe a standing pipeline or a one-shot task?

A pipeline verdict spawns a persistent agent, so a false positive here costs
a long-lived actor for a question that wanted a single reply.
"""

import re


def is_pipeline_request(task: str) -> bool:
    """Detect reactive/persistent pipeline requests vs one-shot tasks.
    Pipelines use conditional/temporal language: if/when/whenever/monitor/watch/notify.
    Also catches explicit spawn/continuous-agent requests like:
      "spawn an agent to log the mean..."
      "create an agent that subscribes to..."
      "I want an agent to send to a topic random temp..."
    """
    lowered = task.lower()

    # Explicit pipeline prefix always wins
    if lowered.startswith(("pipeline:", "pipeline ")):
        return True

    patterns = [
        r"\bif\b.*\bthen\b",
        r"\bif\b.*\b(send|notify|alert|turn|open|close|post|message|say|tell|warn|log|print|publish|emit)\b",
        r"\bwhen\b.*\b(detect|open|turn|send|notify|alert|is|becomes|goes|changes|say|warn|log)\b",
        r"\bwhenever\b",
        r"\bmonitor\b",
        r"\bwatch\b",
        r"\bcheck\b.*\b(every|continuously|periodically|if|when)\b",
        r"\balert me\b",
        r"\bnotify me\b",
        r"\btell me\b.*\bif\b",
        r"\bsend me\b.*\b(when|if|discord|message|notification)\b",
        r"\bsend me a\b",
        r"\bautomatically\b",
        r"\bevery time\b",
        r"\bon detection\b",
        r"\bis turned on\b",
        r"\bis turned off\b",
        r"\bturns on\b",
        r"\bturns off\b",
        r"\bopens\b.*\b(send|notify|alert|light|turn)\b",
        r"\b(door|window|sensor|lamp|light|temperature|humidity|motion)\b.*\b(send|notify|discord|message)\b",
        # camera/detect + action = pipeline
        r"\b(camera|detect|yolo|webcam)\b.*\b(turn|open|send|notify|alert)\b",
        r"\b(person|motion|object)\b.*\bdetect.*\b(turn|open|light|send)\b",
        # ── Spawn / continuous agent / app requests ──
        # "spawn an agent to...", "create an agent that...", "I want an agent to...",
        # "spawn an app that...", "build a service that...", etc.
        # NB: matches "agent" OR "app" OR "bot" OR "service" OR "monitor" OR "rule"
        r"\b(spawn|create|make|start|run|launch|deploy|build|set\s+up)\b.*\b(agent|app|bot|service|monitor|rule|pipeline|listener|handler|watcher)\b",
        r"\b(i\s+want|i\s+need|i'd\s+like)\b.*\b(agent|app|bot|service|rule|something)\b.*\b(to|that|which)\b",
        # Periodic / continuous language
        r"\bevery\s+\d+\s*(sec|min|hour|s\b|m\b|h\b)",
        r"\bcontinuously\b",
        r"\bconstantly\b",
        r"\bperiodically\b",
        r"\bkeep\s+(running|publishing|logging|sending|checking)\b",
        r"\b(subscribe|listen)\s+(to|for|on)\b",
        r"\blog\s+(the|every|each|all)\b",
        # ── Clock-time triggers (5pm, 7am, 17:00, every weekday, etc.) ──
        # These should always be pipelines because they need a ScheduledAgent.
        r"\bat\s+\d{1,2}(:\d{2})?\s*(am|pm)?\b",  # 'at 5pm', 'at 09:30', 'at 7 am'
        r"\b(every|each)\s+(mon|tue|wed|thu|fri|sat|sun|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        r"\b(every|each)\s+(weekday|weekend|day|morning|evening|night|afternoon|hour|minute)\b",
        r"\bdaily\b.*\b(at|on|when)\b",
        r"\bweekly\b",
        r"\bnightly\b",
        r"\btomorrow\b.*\b(at|morning|evening|night)\b",
        r"\bremind\s+me\b",
        r"\bschedule\b.*\b(to|for|every|at)\b",
    ]
    return any(re.search(p, lowered) for p in patterns)
