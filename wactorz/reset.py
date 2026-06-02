"""
wactorz-reset  —  clear stored state without touching running agents.

Scopes
------
chat        chat_log rows + conversation_history / history_summary kv entries
state       per-agent pickle file (state/<name>/state.pkl)
metrics     cost and message-count kv entries
spawns      spawn_registry table
logs        truncate wactorz.log and monitor.log (safe while running)
all         everything above

Each function is safe to call while the system is down (offline reset) or
while it is running (the next heartbeat / restart will repopulate from scratch).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_CHAT_KV_KEYS  = ("conversation_history", "history_summary")
_METRIC_KV_KEYS = ("_final_cost", "_messages_processed")


_DEFAULT_DB    = "./state/wactorz.db"
_DEFAULT_STATE = "./state"


def _db(db_path: Optional[str] = None):
    from wactorz.core.persistence import WactorzDB
    return WactorzDB(db_path or _DEFAULT_DB)


def _pickle_store(state_dir: Optional[str] = None):
    from wactorz.core.persistence import PickleStore
    return PickleStore(state_dir or _DEFAULT_STATE)


# ── public API ────────────────────────────────────────────────────────────────

def reset_chat(agent_name: Optional[str] = None, db_path: Optional[str] = None) -> None:
    """Clear chat_log and conversation kv entries (optionally for one agent)."""
    db = _db(db_path)
    rows = db.clear_chat_log(agent_name)
    logger.info("[reset] chat_log: deleted %d rows%s", rows,
                f" for {agent_name!r}" if agent_name else "")

    agents: list[str] = [agent_name] if agent_name else _all_kv_agents(db)
    for agent in agents:
        for key in _CHAT_KV_KEYS:
            db.kv_delete(agent, key)
    logger.info("[reset] conversation kv cleared%s",
                f" for {agent_name!r}" if agent_name else " (all agents)")


def reset_agent_state(agent_name: str, state_dir: Optional[str] = None) -> None:
    """Delete the pickle state file for one agent."""
    store = _pickle_store(state_dir)
    store.delete(agent_name)
    logger.info("[reset] pickle state deleted for %r", agent_name)


def reset_metrics(agent_name: Optional[str] = None, db_path: Optional[str] = None) -> None:
    """Clear cost and message-count kv entries (optionally for one agent)."""
    db = _db(db_path)
    agents: list[str] = [agent_name] if agent_name else _all_kv_agents(db)
    for agent in agents:
        for key in _METRIC_KV_KEYS:
            db.kv_delete(agent, key)
    logger.info("[reset] metrics kv cleared%s",
                f" for {agent_name!r}" if agent_name else " (all agents)")


def reset_spawns(agent_name: Optional[str] = None, db_path: Optional[str] = None) -> None:
    """Clear the spawn_registry (optionally for one agent)."""
    db = _db(db_path)
    rows = db.clear_spawn_registry(agent_name)
    logger.info("[reset] spawn_registry: deleted %d rows%s", rows,
                f" for {agent_name!r}" if agent_name else "")


def reset_logs(log_dir: Optional[str] = None) -> None:
    """Truncate log files. Safe to call while the system is running."""
    truncated: set[str] = set()

    # Truncate any open FileHandlers in the running process first
    for handler in logging.root.handlers:
        if isinstance(handler, logging.FileHandler):
            try:
                handler.acquire()
                handler.stream.truncate(0)
                handler.stream.seek(0)
                truncated.add(str(Path(handler.baseFilename).resolve()))
                handler.release()
            except Exception as exc:
                logger.warning("[reset] could not truncate handler %s: %s",
                               handler.baseFilename, exc)

    # Also handle files by path (supports offline use)
    base = Path(log_dir or ".")
    for name in ("wactorz.log", "monitor.log"):
        p = (base / name).resolve()
        if p.exists() and str(p) not in truncated:
            try:
                p.write_text("")
                logger.info("[reset] truncated log file: %s", p)
            except Exception as exc:
                logger.warning("[reset] could not truncate %s: %s", p, exc)

    logger.info("[reset] logs cleared")


def reset_all(agent_name: Optional[str] = None,
              db_path: Optional[str] = None,
              state_dir: Optional[str] = None) -> None:
    """Full wipe: chat, metrics, spawns, pickle state, and log files."""
    reset_chat(agent_name, db_path)
    reset_metrics(agent_name, db_path)
    reset_spawns(agent_name, db_path)
    if agent_name:
        reset_agent_state(agent_name, state_dir)
    else:
        _reset_all_pickles(state_dir)
    if not agent_name:
        reset_logs()
    logger.info("[reset] full wipe complete%s",
                f" for {agent_name!r}" if agent_name else "")


# ── helpers ───────────────────────────────────────────────────────────────────

def _all_kv_agents(db) -> list[str]:
    rows = db._conn.execute("SELECT DISTINCT agent FROM kv_store").fetchall()
    return [r[0] for r in rows]


def _reset_all_pickles(state_dir: Optional[str] = None) -> None:
    base = Path(state_dir or _DEFAULT_STATE)
    for pkl in base.glob("*/state.pkl"):
        pkl.unlink(missing_ok=True)
        logger.info("[reset] deleted pickle: %s", pkl)


# ── CLI ───────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wactorz-reset",
        description="Clear wactorz stored state.",
    )
    p.add_argument("--chat",    action="store_true", help="Clear chat log and conversation history")
    p.add_argument("--state",   action="store_true", help="Clear agent pickle state file(s)")
    p.add_argument("--metrics", action="store_true", help="Clear cost and message-count data")
    p.add_argument("--spawns",  action="store_true", help="Clear spawn registry")
    p.add_argument("--logs",    action="store_true", help="Truncate wactorz.log and monitor.log")
    p.add_argument("--all",     action="store_true", help="Clear everything (including logs)")
    p.add_argument("--agent",   metavar="NAME",       help="Limit to a single agent by name")
    p.add_argument("--db",      metavar="PATH",       help="Path to wactorz.db (default: from config)")
    p.add_argument("--state-dir", metavar="PATH",     help="Path to state/ dir (default: ./state)")
    return p


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        stream=sys.stdout)
    args = _build_parser().parse_args()

    if not any([args.chat, args.state, args.metrics, args.spawns, args.logs, args.all]):
        _build_parser().print_help()
        sys.exit(1)

    agent     = args.agent or None
    db_path   = args.db or None
    state_dir = args.state_dir or None

    if args.all:
        reset_all(agent, db_path, state_dir)
        return

    if args.chat:
        reset_chat(agent, db_path)
    if args.state:
        if agent:
            reset_agent_state(agent, state_dir)
        else:
            _reset_all_pickles(state_dir)
    if args.metrics:
        reset_metrics(agent, db_path)
    if args.spawns:
        reset_spawns(agent, db_path)
    if args.logs:
        reset_logs()


if __name__ == "__main__":
    main()
