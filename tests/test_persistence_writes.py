"""
Tests for the real WactorzDB write path against an on-disk SQLite database.

The rest of the suite stubs out persistence with an in-memory fake, so the
actual schema is never exercised. That gap let a schema ship that could not be
written on SQLite < 3.42 (the unixepoch('subsec') DEFAULT). These tests open a
real WactorzDB and write through every public path, plus guard against
reintroducing a version-gated function in the schema.
"""

import os
import sqlite3
import tempfile
import time
import unittest

from wactorz.core.persistence import WactorzDB, _SCHEMA_SQL


class WactorzDBWritePathTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        # mirror cli.py's default relative layout: ./state/wactorz.db
        self.db = WactorzDB(os.path.join(self._dir.name, "state", "wactorz.db"))

    def tearDown(self):
        self.db.close()
        self._dir.cleanup()

    def test_kv_roundtrip(self):
        """kv_set/kv_get must work — this is the path that failed on old SQLite."""
        self.db.kv_set("agent-a", "answer", {"n": 42})
        self.assertEqual(self.db.kv_get("agent-a", "answer"), {"n": 42})
        self.assertEqual(self.db.kv_get("agent-a", "missing", "default"), "default")

    def test_all_write_methods_compile_and_persist(self):
        """Every public write must compile against the live SQLite engine."""
        ts = time.time()
        self.db.write_sensor(ts, "sensors/temp", "sensor.t", "temp", 21.5, unit="C")
        self.db.write_detection(ts, "yolo", "person", 0.9)
        self.db.write_ha_state(ts, "light.kitchen", "off", "on", domain="light")
        self.db.write_actuation(ts, "act", "light", "turn_on", "light.kitchen")
        self.db.write_chat_log(ts, "main", "user", "hello")
        # round-trips read back
        self.assertEqual(len(self.db.query_chat_log("main")), 1)
        self.assertEqual(len(self.db.query_sensor(topic="sensors/temp")), 1)

    def test_default_timestamp_is_populated(self):
        """
        A write that omits the DEFAULT-bearing column forces the DEFAULT
        expression to evaluate; it must succeed and yield ~now.
        """
        conn = self.db.conn
        conn.execute(
            "INSERT INTO kv_store (agent, key, value) VALUES (?, ?, ?)",
            ("agent-b", "k", '"v"'),
        )
        conn.commit()
        updated = conn.execute(
            "SELECT updated FROM kv_store WHERE agent='agent-b' AND key='k'"
        ).fetchone()[0]
        self.assertAlmostEqual(updated, time.time(), delta=5.0)


class SchemaPortabilityTest(unittest.TestCase):
    def test_schema_uses_no_version_gated_functions(self):
        """
        Guard against reintroducing functions with a recent SQLite floor.
        CI's bundled SQLite is modern, so a real-DB test alone would not catch
        this — assert on the schema text directly instead.
        """
        for fn in ("unixepoch", "timediff"):
            self.assertNotRegex(
                _SCHEMA_SQL,
                rf"\b{fn}\s*\(",
                f"{fn}() requires a recent SQLite; use the julianday()-based "
                f"expression for portability",
            )

    def test_schema_creates_on_any_sqlite(self):
        """The full schema must compile on the running SQLite engine."""
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(_SCHEMA_SQL)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
