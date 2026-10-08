"""Databases as older releases left them, for tests of what an upgrade does.

A database made today is created current, so a test of a migration has to make
one that is not: what the first release left behind, before `framework_version`
and the migration history existed.
"""

from wactorz.core.persistence import WactorzDB


def as_first_release(db: WactorzDB) -> WactorzDB:
    """``db`` turned back into what the first release left: version 1, no history.

    The tables the migrations alter are left as they are; every migration checks
    for what it adds before adding it, so it finds nothing to do there and is
    still recorded as applied.
    """
    with db.transaction() as conn:
        conn.execute("DROP TABLE migration_history")
        conn.execute("DROP TABLE schema_version")
        conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_version (version) VALUES (1)")
    return db
