"""
One-time script to clear corrupted conversation_history from AgentFlow persistence.
Run this ONCE from your agentflow directory before restarting:

    python fix_history.py

Then restart AgentFlow normally.
"""
import json
import pathlib
import sys

persist_dir = pathlib.Path("persist")
if not persist_dir.exists():
    persist_dir = pathlib.Path(".agentflow_persist")
if not persist_dir.exists():
    print("No persist directory found — nothing to clean.")
    sys.exit(0)

fixed = 0
for f in persist_dir.glob("*.json"):
    try:
        data = json.loads(f.read_text())
        if "conversation_history" not in data:
            continue

        history = data["conversation_history"]
        clean = []
        for m in history:
            if not isinstance(m, dict):
                continue
            role    = m.get("role", "")
            content = m.get("content", "")
            if role not in ("user", "assistant"):
                continue
            if not isinstance(content, str):
                content = str(content)
            if content.strip():
                clean.append({"role": role, "content": content})

        removed = len(history) - len(clean)
        if removed > 0:
            data["conversation_history"] = clean
            f.write_text(json.dumps(data, indent=2))
            print(f"  Fixed {f.name}: removed {removed} corrupted message(s), kept {len(clean)}")
            fixed += 1
        else:
            print(f"  OK {f.name}: history clean ({len(clean)} messages)")
    except Exception as e:
        print(f"  ERROR {f.name}: {e}")

print(f"\nDone. Fixed {fixed} file(s). You can now restart AgentFlow.")
