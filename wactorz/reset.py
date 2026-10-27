"""wactorz-reset  —  clear stored state without touching running agents.

Scopes
------
chat        chat_log rows + conversation_history / history_summary kv entries,
            and the stored attachments: every one, or for one agent those
            only its thread referred to
state       per-agent pickle file (state/<name>/state.pkl)
metrics     cost and message-count kv entries, and the metrics history
spawns      the spawn registry main restores agents from
logs        truncate wactorz.log and monitor.log (safe while running)
all         everything above, plus the durable memory main keeps in the
            database: pipeline rules, user facts, notification URLs, topic
            contracts and agent manifests

Each function is safe to call while the system is down (offline reset) or
while it is running (the next heartbeat / restart will repopulate from scratch).
"""

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

from wactorz.core.addresses import parse
from wactorz.core.paths import resolve_state_dir
from wactorz.web.uploads import UPLOADS_DIRNAME, discard

logger = logging.getLogger(__name__)

_CHAT_KV_KEYS = ("conversation_history", "history_summary")
_METRIC_KV_KEYS = ("_final_cost", "_messages_processed")
#: Durable memory kept in kv_store and read back through recall() on every
#: call, so nothing in memory stands between a wipe and the rows: pipeline
#: rules, user facts, notification webhook URLs, topic contracts and agent
#: manifests. Each of the other scopes deletes only its own keys, and main is
#: a kept actor, so its rows are never purged wholesale — without this list a
#: "wipe everything" left every rule and fact in place.
_MEMORY_KV_KEYS = (
    "_pipeline_rules",
    "_user_facts",
    "_notification_urls",
    "_topic_contracts",
    "_agent_manifests",
)


# Resolved, not ensured: a wipe must target the same durable location the app
# writes to without creating it as an import side effect.
_DEFAULT_STATE = resolve_state_dir()
_DEFAULT_DB = Path(_DEFAULT_STATE) / "wactorz.db"


def _db(db_path: str | None = None):
    from wactorz.core.persistence import WactorzDB

    return WactorzDB(db_path or _DEFAULT_DB)


def _pickle_store(state_dir: str | None = None):
    from wactorz.core.persistence import PickleStore

    return PickleStore(state_dir or _DEFAULT_STATE)


# ── public API ────────────────────────────────────────────────────────────────


def reset_uploads(state_dir: str | None = None) -> None:
    """Delete every stored chat attachment, for a clear of every thread.

    Leaving them behind is worse than it sounds: the rows referencing them are
    what has just been deleted, so afterwards nothing can tell which bytes are
    still live, including someone reading the directory by hand.
    """
    # Not `upload_dir()`: that creates the directory, and a wipe must not bring
    # a state directory into being on a machine that has none.
    base = Path(state_dir or _DEFAULT_STATE) / UPLOADS_DIRNAME
    if not base.is_dir():
        return
    removed = 0
    for path in base.iterdir():
        try:
            path.unlink()
            removed += 1
        except OSError as exc:
            logger.warning("[reset] could not remove %s: %s", path, exc)
    logger.info("[reset] uploads: deleted %d files", removed)


def reset_chat(
    agent_name: str | None = None,
    db_path: str | None = None,
    state_dir: str | None = None,
) -> None:
    """Clear chat_log and conversation kv entries (optionally for one agent).

    The stored attachments go with the rows that sent them. A row is filed
    under its thread whoever attached the file, the person or the agent, so one
    agent's files are those its rows name and no other thread's row does; a
    full clear takes them all.
    """
    with _db(db_path) as db:
        # Read before the rows go: afterwards nothing says which files they named.
        named = db.chat_attachment_ids(agent_name) if agent_name else set()
        rows = db.clear_chat_log(agent_name)
        orphaned = named - db.chat_attachment_ids()
        logger.info(
            "[reset] chat_log: deleted %d rows%s",
            rows,
            f" for {agent_name!r}" if agent_name else "",
        )

        agents: list[str] = [agent_name] if agent_name else _all_kv_agents(db)
        for agent in agents:
            for key in _CHAT_KV_KEYS:
                db.kv_delete(agent, key)
    logger.info(
        "[reset] conversation kv cleared%s",
        f" for {agent_name!r}" if agent_name else " (all agents)",
    )
    _strip_chat_from_pickles(agent_name, state_dir)
    if agent_name:
        gone = discard(orphaned, state_dir)
        logger.info("[reset] uploads: deleted %d files for %r", gone, agent_name)
    else:
        reset_uploads(state_dir)


def reset_agent_state(agent_name: str, state_dir: str | None = None) -> None:
    """Delete the pickle state file for one agent."""
    store = _pickle_store(state_dir)
    store.delete(agent_name)
    logger.info("[reset] pickle state deleted for %r", agent_name)


def reset_metrics(agent_name: str | None = None, db_path: str | None = None) -> None:
    """Clear cost and message-count kv entries, and the metrics history (optionally for one agent)."""
    with _db(db_path) as db:
        # The history is a record of these same counters over time; a reset that
        # zeroed them and kept their past would show a cliff in every trend.
        db.clear_metrics_history(agent_name)
        agents: list[str] = [agent_name] if agent_name else _all_kv_agents(db)
        for agent in agents:
            for key in _METRIC_KV_KEYS:
                db.kv_delete(agent, key)
        # The monitor's durable lifetime cost ledger (keyed by actor_id under
        # the _system agent) is monotonic and outlives individual agents, so a
        # full metrics reset must clear it too or the headline total never
        # zeroes. A single-agent reset can't map name->actor_id here, so it's
        # left intact.
        if not agent_name:
            db.kv_delete("_system", "_lifetime_cost_ledger")
    logger.info(
        "[reset] metrics kv cleared%s", f" for {agent_name!r}" if agent_name else " (all agents)"
    )


def reset_memory(agent_name: str | None = None, db_path: str | None = None) -> None:
    """Clear pipeline rules, user facts, notification URLs, topic contracts and
    agent manifests (optionally for one agent).

    These live in ``kv_store`` and are read through ``recall()`` on every use,
    so clearing an in-memory attribute does nothing: the next read comes
    straight back from the database. Part of a full wipe only — no scope of
    its own — because a reset that forgets the agents but keeps the rules
    that spawned them is exactly the half-reset this exists to prevent.
    """
    with _db(db_path) as db:
        agents: list[str] = [agent_name] if agent_name else _all_kv_agents(db)
        for agent in agents:
            for key in _MEMORY_KV_KEYS:
                db.kv_delete(agent, key)
    logger.info(
        "[reset] memory kv cleared%s", f" for {agent_name!r}" if agent_name else " (all agents)"
    )


#: Where main keeps its spawn registry: by actor id, and, in a store from before
#: ids were kept, by name. Main reads the second when the first is absent, so a
#: reset clears both, or a cleared registry would come back from the older one.
_SPAWN_REGISTRY_KV_KEYS = ("_spawned_agents_by_id", "_spawned_agents")


def _without_agent(registry: dict[str, Any], agent_name: str) -> dict[str, Any] | None:
    """``registry`` without the agent ``agent_name``, or None when it holds none.

    Kept by id, an entry carries its name; kept by name, the key is the name.
    """
    kept = {
        key: entry
        for key, entry in registry.items()
        if key != agent_name and not (isinstance(entry, dict) and entry.get("name") == agent_name)
    }
    return kept if len(kept) != len(registry) else None


def reset_spawns(agent_name: str | None = None, db_path: str | None = None) -> None:
    """Clear the spawn registry (optionally for one agent).

    The registry lives in ``kv_store`` under (owner, key), which is what
    ``main._restore_spawned_agents()`` reads at start, so an agent left in it is
    spawned again on the next restart. ``agent_name`` here is the *spawned*
    agent's name, and every entry of that name is cleared.
    """
    with _db(db_path) as db:
        cleared: set[str] = set()
        # One transaction: each owner's entry is read, edited and written back,
        # and an agent persisting its own registry between the read and the
        # write would otherwise have that write clobbered — or clobber this one.
        with db.transaction():
            for owner in _all_kv_agents(db):
                for key in _SPAWN_REGISTRY_KV_KEYS:
                    reg = db.kv_get(owner, key, None)
                    if not isinstance(reg, dict):
                        continue
                    if agent_name:
                        kept = _without_agent(reg, agent_name)
                        if kept is not None:
                            db.kv_set(owner, key, kept)
                            cleared.add(owner)
                    else:
                        db.kv_delete(owner, key)
                        cleared.add(owner)
    logger.info(
        "[reset] kv spawn registry cleared for %d owner(s)%s",
        len(cleared),
        f" (agent {agent_name!r})" if agent_name else "",
    )


def reset_logs(log_dir: str | None = None) -> None:
    """Truncate log files. Safe to call while the system is running."""
    truncated: set[str] = set()

    # Truncate any open FileHandlers in the running process first
    for handler in logging.root.handlers:
        if isinstance(handler, logging.FileHandler):
            try:
                handler.acquire()
                # The release must be guaranteed, not merely the next statement:
                # this failure is caught and logged below, so a stranded lock
                # would leave the reset reporting success while every later log
                # call blocks forever — with nothing in the log to say why,
                # because logging is what deadlocked.
                try:
                    if handler.stream is not None:  # None for a delay-opened handler
                        handler.stream.truncate(0)
                        handler.stream.seek(0)
                        truncated.add(str(Path(handler.baseFilename).resolve()))
                finally:
                    handler.release()
            except Exception as exc:
                logger.warning(
                    "[reset] could not truncate handler %s: %s", handler.baseFilename, exc
                )

    # Also handle files by path (supports offline use). Defaults to the resolved
    # state directory, which is where log_setup writes the log — a cwd default
    # would truncate nothing whenever the wipe runs from a different directory.
    base = Path(log_dir or _DEFAULT_STATE)
    for name in ("wactorz.log", "monitor.log"):
        p = (base / name).resolve()
        if p.exists() and str(p) not in truncated:
            try:
                p.write_text("")
                logger.info("[reset] truncated log file: %s", p)
            except Exception as exc:
                logger.warning("[reset] could not truncate %s: %s", p, exc)

        # Rotated backups (wactorz.log.1, .2, …). Deleted rather than truncated:
        # nothing holds them open, and an empty backup is only clutter. Without
        # this a wipe leaves every earlier generation of the log in place, which
        # is the opposite of what "reset" promises — redaction covers known
        # secret shapes, so the ones it misses are exactly what stays behind.
        for backup in sorted(base.glob(f"{name}.*")):
            try:
                backup.unlink()
                logger.info("[reset] removed rotated log: %s", backup)
            except Exception as exc:
                logger.warning("[reset] could not remove %s: %s", backup, exc)

    logger.info("[reset] logs cleared")


def reset_all(
    agent_name: str | None = None, db_path: str | None = None, state_dir: str | None = None
) -> None:
    """Full wipe: chat, metrics, spawns, memory, pickle state, attachments, and logs."""
    reset_chat(agent_name, db_path, state_dir)
    reset_metrics(agent_name, db_path)
    reset_spawns(agent_name, db_path)
    reset_memory(agent_name, db_path)
    if agent_name:
        reset_agent_state(agent_name, state_dir)
    else:
        _reset_all_pickles(state_dir)
    if not agent_name:
        reset_logs()
    logger.info("[reset] full wipe complete%s", f" for {agent_name!r}" if agent_name else "")


# ── helpers ───────────────────────────────────────────────────────────────────


def _all_kv_agents(db) -> list[str]:
    rows = db._conn.execute("SELECT DISTINCT agent FROM kv_store").fetchall()
    return [r[0] for r in rows]


def _strip_chat_from_pickles(agent_name: str | None, state_dir: str | None = None) -> None:
    """Remove the conversation keys from any legacy `state.pkl` that holds them.

    Clearing the database is not enough on its own. An agent's `state.pkl`
    predates the per-key store and can hold its own `conversation_history`,
    which the start-up migration copies into the database whenever the
    database has none -- so a conversation cleared only there comes back on
    the next restart.

    Driven off the files on disk rather than the agents with database rows: the
    case that motivated this had a pickle and no rows at all, so a
    database-driven loop would have walked straight past it.

    Only rewrites a file something was actually removed from. That is also what
    makes it safe for an unreadable one: `PickleStore.load` quarantines it and
    returns an empty dict, which contains none of these keys, so it is skipped
    rather than replaced with the empty dict.
    """
    store = _pickle_store(state_dir)
    base = Path(state_dir or _DEFAULT_STATE)
    names = [agent_name] if agent_name else [p.parent.name for p in base.glob("*/state.pkl")]
    for name in names:
        state = store.load(name)
        if not any(key in state for key in _CHAT_KV_KEYS):
            continue
        for key in _CHAT_KV_KEYS:
            # One key at a time, so the store keeps any value it could not read.
            store.remove(name, key)
        logger.info("[reset] conversation removed from legacy state file for %r", name)


def _reset_all_pickles(state_dir: str | None = None) -> None:
    """Delete every agent's state file.

    Through the store, not by removing the files: a running server keeps each
    state in memory and writes it back, so a file removed behind it returns
    with the next thing that agent persists.
    """
    store = _pickle_store(state_dir)
    base = Path(state_dir or _DEFAULT_STATE)
    for pkl in sorted(base.glob("*/state.pkl")):
        store.delete(pkl.parent.name)
        logger.info("[reset] deleted pickle: %s", pkl)


# ── CLI ───────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wactorz-reset",
        description="Clear wactorz stored state.",
    )
    p.add_argument("--chat", action="store_true", help="Clear chat log and conversation history")
    p.add_argument("--state", action="store_true", help="Clear agent pickle state file(s)")
    p.add_argument("--metrics", action="store_true", help="Clear cost and message-count data")
    p.add_argument("--spawns", action="store_true", help="Clear spawn registry")
    p.add_argument("--logs", action="store_true", help="Truncate wactorz.log and monitor.log")
    p.add_argument("--all", action="store_true", help="Clear everything (including logs)")
    p.add_argument("--agent", metavar="NAME", help="Limit to a single agent, by name or name@node")
    p.add_argument("--db", metavar="PATH", help="Path to wactorz.db (default: from config)")
    p.add_argument("--state-dir", metavar="PATH", help="Path to state/ dir (default: ./state)")
    return p


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout
    )
    args = _build_parser().parse_args()

    if not any([args.chat, args.state, args.metrics, args.spawns, args.logs, args.all]):
        _build_parser().print_help()
        sys.exit(1)

    # What is stored here is kept by the agent's name, whichever node it runs on.
    agent = parse(args.agent).name if args.agent else None
    db_path = args.db or None
    state_dir = args.state_dir or None

    if args.all:
        reset_all(agent, db_path, state_dir)
        return

    if args.chat:
        reset_chat(agent, db_path, state_dir)
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
