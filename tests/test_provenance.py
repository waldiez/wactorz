"""The provenance graph: nodes joined from cause to effect, and the walks over it.

Against a real on-disk SQLite database, since the walks are recursive queries
and the point is that SQLite answers them; a fake would answer from whatever it
was told. Also the upgrade: a database made before the graph gains its tables.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.old_database import as_first_release
from wactorz.core.persistence import WactorzDB
from wactorz.core.persistence.migrations import (
    FRAMEWORK_VERSION,
    get_current_version,
    run_migrations,
)
from wactorz.core.persistence.pickle_store import PickleStore
from wactorz.core.provenance import (
    ACTION,
    DECISION,
    DONE,
    EVALUATED_BY,
    INVALIDATED,
    LABEL,
    LED_TO,
    MODEL,
    OUTCOME,
    PLANNED,
    PRODUCED,
    QUESTION,
    SUPERSEDED_BY,
    Provenance,
    ProvenanceError,
    stable_id,
)

T0 = 1_760_000_000.0


@pytest.fixture(name="db")
def db_fixture(tmp_path: Path) -> Iterator[WactorzDB]:
    with WactorzDB(tmp_path / "state" / "wactorz.db") as database:
        yield database


@pytest.fixture(name="graph")
def graph_fixture(db: WactorzDB) -> Provenance:
    return Provenance(db)


class TestNodes:
    def test_a_node_starts_with_the_first_status_its_kind_may_hold(self, graph: Provenance) -> None:
        action = graph.add_node(ACTION, ts=T0, actor="agent:controller")
        label = graph.add_node(LABEL, ts=T0, source="sensors/ahu-1")

        assert action.status == PLANNED
        assert label.status == "valid"

    def test_a_node_reads_back_as_written(self, graph: Provenance) -> None:
        written = graph.add_node(
            DECISION,
            ts=T0,
            source="sensors/ahu-1",
            actor="agent:controller",
            payload={"choice": "retrain"},
        )

        read = graph.get(written.id)

        assert read == written
        assert read is not None and read.payload == {"choice": "retrain"}

    def test_a_node_written_twice_under_one_id_is_written_once(self, graph: Provenance) -> None:
        node_id = stable_id("question", "hvac-watch", "q1")
        first = graph.add_node(QUESTION, ts=T0, node_id=node_id, payload={"text": "Changed?"})

        again = graph.add_node(QUESTION, ts=T0 + 60, node_id=node_id, payload={"text": "other"})

        assert again == first
        assert len(graph.nodes(QUESTION)) == 1

    def test_an_unknown_kind_is_refused(self, graph: Provenance) -> None:
        with pytest.raises(ProvenanceError, match="unknown node kind"):
            graph.add_node("guess", ts=T0)

    def test_a_status_the_kind_cannot_hold_is_refused(self, graph: Provenance) -> None:
        with pytest.raises(ProvenanceError, match="cannot be"):
            graph.add_node(LABEL, ts=T0, status=DONE)

    def test_only_the_status_of_a_node_changes(self, graph: Provenance) -> None:
        action = graph.add_node(ACTION, ts=T0, payload={"what": "retrain"})

        changed = graph.set_status(action.id, DONE)

        assert changed.status == DONE
        stored = graph.get(action.id)
        assert (
            stored is not None and stored.status == DONE and stored.payload == {"what": "retrain"}
        )

    def test_a_node_knows_the_order_it_was_written_in(self, graph: Provenance) -> None:
        first = graph.add_node(LABEL, ts=T0, recorded=T0)
        second = graph.add_node(LABEL, ts=T0, recorded=T0)

        assert first.seq < second.seq
        read = graph.get(second.id)
        assert read is not None and read.seq == second.seq

    def test_a_window_and_a_moment_read_back(self, graph: Provenance) -> None:
        window = graph.add_node(LABEL, ts=T0, ts_end=T0 + 60)
        moment = graph.add_node(QUESTION, ts=T0, ts_end=T0)
        open_ = graph.add_node(LABEL, ts=T0)

        assert (graph.get(window.id) or window).ts_end == T0 + 60
        assert (graph.get(moment.id) or moment).ts_end == T0
        assert (graph.get(open_.id) or open_).ts_end is None

    def test_stable_ids_repeat_and_differ_by_their_parts(self) -> None:
        assert stable_id("label", "a", "q1") == stable_id("label", "a", "q1")
        assert stable_id("label", "a", "q1") != stable_id("label", "a", "q2")


class TestEdges:
    def test_an_edge_joins_two_nodes_that_exist(self, graph: Provenance) -> None:
        decision = graph.add_node(DECISION, ts=T0)
        action = graph.add_node(ACTION, ts=T0)

        graph.add_edge(decision.id, action.id, LED_TO)

        (edge,) = graph.edges(action.id)
        assert (edge.src, edge.dst, edge.relation) == (decision.id, action.id, LED_TO)

    def test_an_edge_added_twice_is_one_edge(self, graph: Provenance) -> None:
        decision = graph.add_node(DECISION, ts=T0)
        action = graph.add_node(ACTION, ts=T0)

        graph.add_edge(decision.id, action.id, LED_TO)
        graph.add_edge(decision.id, action.id, LED_TO)

        assert len(graph.edges(decision.id)) == 1

    def test_an_edge_to_a_missing_node_is_refused(self, graph: Provenance) -> None:
        decision = graph.add_node(DECISION, ts=T0)

        with pytest.raises(ProvenanceError, match="no node"):
            graph.add_edge(decision.id, "nowhere", LED_TO)

    def test_an_unknown_relation_is_refused(self, graph: Provenance) -> None:
        a, b = graph.add_node(DECISION, ts=T0), graph.add_node(ACTION, ts=T0)

        with pytest.raises(ProvenanceError, match="unknown relation"):
            graph.add_edge(a.id, b.id, "caused")

    def test_a_relation_joins_only_the_kinds_it_is_for(self, graph: Provenance) -> None:
        decision, action = graph.add_node(DECISION, ts=T0), graph.add_node(ACTION, ts=T0)

        with pytest.raises(ProvenanceError, match="does not join"):
            graph.add_edge(decision.id, action.id, SUPERSEDED_BY)

    def test_a_node_cannot_cause_itself(self, graph: Provenance) -> None:
        decision = graph.add_node(DECISION, ts=T0)

        with pytest.raises(ProvenanceError, match="cannot cause itself"):
            graph.add_edge(decision.id, decision.id, LED_TO)


class TestWalks:
    def episode(self, graph: Provenance) -> dict[str, str]:
        """An answer that led to a decision, a retrain, a model and an outcome."""
        label = graph.add_node(LABEL, ts=T0, source="sensors/ahu-1", recorded=T0 + 1)
        decision = graph.add_node(DECISION, ts=T0, recorded=T0 + 2)
        retrain = graph.add_node(ACTION, ts=T0, recorded=T0 + 3)
        model = graph.add_node(MODEL, ts=T0, recorded=T0 + 4)
        outcome = graph.add_node(OUTCOME, ts=T0, recorded=T0 + 5)
        graph.add_edge(label.id, decision.id, LED_TO)
        graph.add_edge(decision.id, retrain.id, LED_TO)
        graph.add_edge(retrain.id, model.id, PRODUCED)
        graph.add_edge(model.id, outcome.id, EVALUATED_BY)
        return {
            "label": label.id,
            "decision": decision.id,
            "retrain": retrain.id,
            "model": model.id,
            "outcome": outcome.id,
        }

    def test_walking_down_finds_everything_that_followed(self, graph: Provenance) -> None:
        ids = self.episode(graph)

        found = [n.id for n in graph.descendants(ids["label"])]

        assert found == [ids["decision"], ids["retrain"], ids["model"], ids["outcome"]]

    def test_walking_up_explains_how_something_came_to_be(self, graph: Provenance) -> None:
        ids = self.episode(graph)

        found = [n.id for n in graph.ancestors(ids["model"])]

        assert found == [ids["label"], ids["decision"], ids["retrain"]]

    def test_a_walk_can_be_kept_short(self, graph: Provenance) -> None:
        ids = self.episode(graph)

        found = [n.id for n in graph.descendants(ids["label"], depth=1)]

        assert found == [ids["decision"]]

    def test_a_cycle_does_not_make_a_walk_endless(self, graph: Provenance) -> None:
        a, b = graph.add_node(DECISION, ts=T0), graph.add_node(ACTION, ts=T0)
        graph.add_edge(a.id, b.id, LED_TO)
        graph.add_edge(b.id, a.id, LED_TO)

        assert [n.id for n in graph.descendants(a.id)] == [b.id]

    def test_nodes_filter_by_the_window_they_touch(self, graph: Provenance) -> None:
        before = graph.add_node(LABEL, ts=T0, ts_end=T0 + 10, source="s")
        inside = graph.add_node(LABEL, ts=T0 + 20, ts_end=T0 + 30, source="s")
        open_ = graph.add_node(LABEL, ts=T0 + 5, source="s")
        moment_out = graph.add_node(QUESTION, ts=T0 + 50, ts_end=T0 + 50, source="s")
        after = graph.add_node(LABEL, ts=T0 + 40, ts_end=T0 + 60, source="s")

        found = {n.id for n in graph.nodes(source="s", since=T0 + 15, until=T0 + 40)}

        assert found == {inside.id, open_.id}
        assert before.id not in found and after.id not in found and moment_out.id not in found

    def test_a_listing_without_a_limit_returns_every_node(self, graph: Provenance) -> None:
        for _ in range(5):
            graph.add_node(LABEL, ts=T0, source="s")

        assert len(graph.nodes(LABEL, source="s", limit=2)) == 2
        assert len(graph.nodes(LABEL, source="s", limit=None)) == 5

    def test_nodes_filter_by_kind_source_and_status(self, graph: Provenance) -> None:
        graph.add_node(LABEL, ts=T0, source="sensors/ahu-1")
        kept = graph.add_node(LABEL, ts=T0, source="sensors/ahu-2")
        gone = graph.add_node(LABEL, ts=T0, source="sensors/ahu-2")
        graph.set_status(gone.id, INVALIDATED)

        found = graph.nodes(LABEL, source="sensors/ahu-2", statuses=("valid",))

        assert [n.id for n in found] == [kept.id]


class TestUpgrade:
    def test_a_new_database_has_the_graph(self, db: WactorzDB) -> None:
        tables = {
            r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }

        assert {"prov_nodes", "prov_edges"} <= tables

    def test_an_older_database_gains_the_graph_when_it_is_upgraded(self, tmp_path: Path) -> None:
        with WactorzDB(tmp_path / "old.db") as database:
            as_first_release(database)
            with database.transaction() as conn:
                conn.execute("DROP TABLE prov_edges")
                conn.execute("DROP TABLE prov_nodes")

            run_migrations(database, PickleStore(str(tmp_path / "state")))

            assert get_current_version(database) == FRAMEWORK_VERSION
            graph = Provenance(database)
            node = graph.add_node(LABEL, ts=T0, source="sensors/ahu-1")
            assert graph.get(node.id) == node
