"""Show the latest N entries from any table in wactorz.db that has a ts column.
Timestamps are formatted from the actual stored unix-time value.

Usage:
    python latest.py                           # default: ha_state_changes, 20 rows
    python latest.py --table chat_log
    python latest.py --table ha_state_changes --n 50
    python latest.py --all                     # latest from every table with ts
"""

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def fmt(ts: float) -> str:
    """The stored unix time as local wall-clock time, to the millisecond."""
    local_dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()
    return local_dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def quote_ident(name: str) -> str:
    """`name` as an SQL identifier, with any double quote inside it doubled.

    SQLite binds values, never table or column names, so a name that has to go
    into a statement is quoted instead of passed as a parameter.
    """
    return '"' + name.replace('"', '""') + '"'


def columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """The column names of `table`, or an empty list when there is no such table."""
    return [row[0] for row in conn.execute("SELECT name FROM pragma_table_info(?)", (table,))]


def show(conn: sqlite3.Connection, table: str, n: int) -> None:
    col_names = columns(conn, table)
    if not col_names:
        print(f"  ({table} not found)")
        return
    if "ts" not in col_names:
        print(f"  ({table} has no `ts` column — columns: {col_names})")
        return

    # Every name below is one SQLite just reported and is quoted, so the
    # f-strings build identifiers, never values: S608 cannot tell the two apart.
    source = quote_ident(table)
    count = conn.execute(f"SELECT COUNT(*) FROM {source}").fetchone()[0]  # noqa: S608
    print(f"\n=== {table} — {count} rows total, showing latest {min(n, count)} ===")
    if count == 0:
        return

    other = [c for c in col_names if c != "ts"][:5]  # first 5 non-ts columns
    sel = ", ".join(quote_ident(c) for c in ["ts", *other])
    query = f"SELECT {sel} FROM {source} ORDER BY ts DESC LIMIT ?"  # noqa: S608
    rows = conn.execute(query, (n,)).fetchall()

    for r in rows:
        ts = r[0]
        rest = " | ".join(f"{name}={str(v)[:50]}" for name, v in zip(other, r[1:], strict=True))
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
            if "ts" in columns(conn, name):
                show(conn, name, args.n)
    else:
        show(conn, args.table, args.n)

    conn.close()


if __name__ == "__main__":
    main()
