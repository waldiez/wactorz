# SPDX-License-Identifier: Apache-2.0
"""The agents the key-value store holds keys for, as a reset of everyone walks them."""

from pathlib import Path

from wactorz.core.persistence import WactorzDB


def test_every_agent_with_a_key_is_named_once(tmp_path: Path) -> None:
    db = WactorzDB(str(tmp_path / "wactorz.db"))
    try:
        db.kv_set("counter", "count", 3)
        db.kv_set("counter", "history", [1, 2])
        db.kv_set("weather", "city", "Thessaloniki")

        assert sorted(db.kv_agents()) == ["counter", "weather"]
    finally:
        db.close()


def test_an_empty_store_names_none(tmp_path: Path) -> None:
    db = WactorzDB(str(tmp_path / "wactorz.db"))
    try:
        assert db.kv_agents() == []
    finally:
        db.close()
