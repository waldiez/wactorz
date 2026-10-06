"""Every table retention prunes is searched by time through an index.

Retention deletes old rows in batches by ``ts``, and the time-series queries ask
for a recent window. Without an index on ``ts`` each batch, and each such query,
reads the whole table — and these are the tables that grow with every reading
and every Home Assistant state change. Checked against SQLite's own query plan,
so the test fails on the plan rather than on a slow machine.
"""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from wactorz.core.persistence import WactorzDB

#: The tables `WactorzDB` prunes by age.
PRUNED = ("sensor_readings", "detections", "ha_state_changes", "actuations", "chat_log")


@pytest.fixture(name="db")
def db_fixture(tmp_path: Path) -> Iterator[WactorzDB]:
    with WactorzDB(str(tmp_path / "wactorz.db")) as database:
        yield database


def _plan(conn: sqlite3.Connection, sql: str, *params: object) -> list[str]:
    return [str(tuple(row)[-1]) for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}", params)]


def _scans(plan: list[str]) -> list[str]:
    """The steps that read a whole table or index, rather than seek into one."""
    return [step for step in plan if step.startswith("SCAN")]


@pytest.mark.parametrize("table", PRUNED)
def test_the_retention_delete_seeks_by_time(db: WactorzDB, table: str) -> None:
    plan = _plan(
        db.conn,
        f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} WHERE ts < ? LIMIT ?)",
        1.0,
        1000,
    )

    assert not _scans(plan), plan


def test_recent_home_assistant_changes_are_read_in_order_without_a_sort(db: WactorzDB) -> None:
    plan = _plan(
        db.conn,
        "SELECT * FROM ha_state_changes WHERE ts >= ? ORDER BY ts ASC LIMIT ?",
        1.0,
        10,
    )

    assert not _scans(plan), plan
    assert not [step for step in plan if "TEMP B-TREE" in step], plan


def test_a_database_created_before_the_index_gains_it_when_opened(tmp_path: Path) -> None:
    path = tmp_path / "older.db"
    with WactorzDB(str(path)) as db:
        db.conn.execute("DROP INDEX idx_ha_ts")
        db.conn.commit()

    with WactorzDB(str(path)) as db:
        names = {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}

    assert "idx_ha_ts" in names
