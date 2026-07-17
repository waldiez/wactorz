"""
Show the latest N entries from any table in wactorz.db that has a ts column.
Timestamps are formatted from the actual stored unix-time value.

Usage:
    python latest.py                           # default: ha_state_changes, 20 rows
    python latest.py --table chat_log
    python latest.py --table ha_state_changes --n 50
    python latest.py --all                     # latest from every table with ts
"""

from __future__ import annotations
import sqlite3
import argparse
from datetime import datetime
from pathlib import Path


def fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def show(conn: sqlite3.Connection, table: str, n: int) -> None:
    cols_info = list(conn.execute(f"PRAGMA table_info({table})"))
    if not cols_info:
        print(f"  ({table} not found)")
        return
    col_names = [c[1] for c in cols_info]
    if "ts" not in col_names:
        print(f"  ({table} has no `ts` column — columns: {col_names})")
        return

    count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    print(f"\n=== {table} — {count} rows total, showing latest {min(n, count)} ===")
    if count == 0:
        return

    other = [c for c in col_names if c != "ts"][:5]  # first 5 non-ts columns
    sel = "ts," + ",".join(other)
    rows = conn.execute(f"SELECT {sel} FROM {table} ORDER BY ts DESC LIMIT ?", (n,)).fetchall()

    for r in rows:
        ts = r[0]
        rest = " | ".join(f"{name}={str(v)[:50]}" for name, v in zip(other, r[1:]))
        print(f"  {fmt(ts)}   {rest}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="./state/wactorz.db")
    p.add_argument("--table", default="ha_state_changes")
    p.add_argument("--n", type=int, default=20)
    p.add_argument("--all", action="store_true", help="Show every table that has a ts column")
    args = p.parse_args()

    if not Path(args.db).exists():
        raise SystemExit(f"DB not found: {args.db}")

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    if args.all:
        names = [
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        ]
        for name in names:
            cols = [c[1] for c in conn.execute(f"PRAGMA table_info({name})")]
            if "ts" in cols:
                show(conn, name, args.n)
    else:
        show(conn, args.table, args.n)

    conn.close()


if __name__ == "__main__":
    main()
