"""A node sends main the files its recorder closed, and main files them where the record is.

The node sends a notice and the bytes on the blob path, again if main does not
answer, and keeps its copy until main has and a retention has passed. Main
checks the hash as it does for every blob, puts the file under the source and
hour the notice names inside its own data directory, never anywhere else, and
answers on a signed topic. ``docs/design/data-storage.md`` is the decision.
"""

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest

from wactorz.agents.main.data_files import DataFileError, DataFiles, next_free, placement
from wactorz.agents.main.hosts import ArtifactHost
from wactorz.core.actor import ActorState
from wactorz.core.blob_transfer import Inbox, sha256_of_topic
from wactorz.core.node_signing import CONTROL_LEAVES
from wactorz.core.paths import resolve_state_dir
from wactorz.data import DataError, data_root
from wactorz.data.parquet import Writer, frame
from wactorz.node.data_shipping import KEEP_AFTER_ACK_S, RESEND_AFTER_S, DataShipper, sha256_of

T0 = 1_760_000_000.0


def _recorded(root: Path, source: str, count: int = 2) -> list[Path]:
    writer = Writer(root, source)
    for i in range(count):
        writer.append({"ts": T0 + i, "arrived": T0 + i, "time_source": "payload", "x": i})
    return writer.flush()


# ── The node ──────────────────────────────────────────────────────────────────


class _Published:
    def __init__(self) -> None:
        self.messages: list[tuple[str, Any]] = []

    async def __call__(self, topic: str, data: Any, **_kw: Any) -> None:
        self.messages.append((topic, data))

    def notices(self) -> list[dict[str, Any]]:
        return [d for t, d in self.messages if t.endswith("/data_file")]

    def blobs(self) -> list[str]:
        return [t for t, _ in self.messages if "/blob_return/" in t]


@pytest.fixture(name="published")
def published_fixture() -> _Published:
    return _Published()


@pytest.fixture(name="shipper")
def shipper_fixture(tmp_path: Path, published: _Published) -> DataShipper:
    return DataShipper("rpi", tmp_path / "node", published, every=0.01)


class TestTheNodeSends:
    async def test_a_closed_file_is_announced_then_sent_once(
        self, shipper: DataShipper, published: _Published
    ) -> None:
        (path,) = _recorded(shipper.root, "sensors/imu/left")

        sent = await shipper.look()
        again = await shipper.look()

        assert sent == ["sensors/imu/left/2025/10/09/08.parquet"]
        assert again == []
        (notice,) = published.notices()
        assert (notice["source"], notice["path"], notice["size"]) == (
            "sensors/imu/left",
            "sensors/imu/left/2025/10/09/08.parquet",
            path.stat().st_size,
        )
        assert notice["sha256"] == sha256_of(path)
        assert published.blobs() == [f"nodes/rpi/blob_return/{notice['sha256']}"]
        assert path.exists()

    async def test_an_unanswered_file_is_sent_again_later(
        self, shipper: DataShipper, published: _Published
    ) -> None:
        _recorded(shipper.root, "sensors/imu/left")
        await shipper.look()
        sent_at = shipper._load()["sensors/imu/left/2025/10/09/08.parquet"]["sent_at"]

        assert shipper.due(now=sent_at + RESEND_AFTER_S - 1) == []
        assert [p.name for p in shipper.due(now=sent_at + RESEND_AFTER_S + 1)] == ["08.parquet"]

    async def test_mains_yes_stops_the_resending_and_the_file_goes_after_the_retention(
        self, shipper: DataShipper, published: _Published
    ) -> None:
        (path,) = _recorded(shipper.root, "sensors/imu/left")
        await shipper.look()
        (notice,) = published.notices()

        key = shipper.acknowledge(notice["sha256"], True)

        assert key == "sensors/imu/left/2025/10/09/08.parquet"
        acked_at = shipper._load()[key]["acked_at"]
        assert shipper.due(now=acked_at + 10 * RESEND_AFTER_S) == []
        assert shipper.trim(now=acked_at + KEEP_AFTER_ACK_S - 1) == []
        assert path.exists()
        assert shipper.trim(now=acked_at + KEEP_AFTER_ACK_S + 1) == [key]
        assert not path.exists()
        assert key not in shipper._load()

    async def test_mains_no_has_it_sent_again_at_the_next_look(
        self, shipper: DataShipper, published: _Published
    ) -> None:
        _recorded(shipper.root, "sensors/imu/left")
        await shipper.look()
        (notice,) = published.notices()

        shipper.acknowledge(notice["sha256"], False, "the file did not arrive")
        sent = await shipper.look()

        assert sent == ["sensors/imu/left/2025/10/09/08.parquet"]
        assert len(published.notices()) == 2

    async def test_an_answer_for_nothing_sent_is_nothing(self, shipper: DataShipper) -> None:
        assert shipper.acknowledge("a" * 64, True) is None

    async def test_the_record_survives_a_new_shipper(
        self, tmp_path: Path, published: _Published
    ) -> None:
        first = DataShipper("rpi", tmp_path / "node", published)
        _recorded(first.root, "sensors/imu/left")
        await first.look()

        second = DataShipper("rpi", tmp_path / "node", published)

        assert await second.look() == []

    def test_the_ack_is_a_signed_control_topic(self) -> None:
        assert "data_ack" in CONTROL_LEAVES


# ── Main ──────────────────────────────────────────────────────────────────────


class _Migration:
    def __init__(self, inbox: Inbox) -> None:
        self.inbox = inbox


class _Host:
    """Main, as the receiver sees it: a name, a state, the inbox and the broker."""

    def __init__(self, inbox: Inbox) -> None:
        self.name = "main"
        self.actor_id = "main-id"
        self.state = ActorState.RUNNING
        self._mqtt_broker = "localhost"
        self._mqtt_port = 1883
        self.migration = _Migration(inbox)
        self.published: list[tuple[str, Any, int]] = []

    async def _mqtt_publish(
        self, topic: str, payload: Any, retain: bool = False, qos: int = 0
    ) -> None:
        self.published.append((topic, payload, qos))

    def acks(self) -> list[dict[str, Any]]:
        return [p for t, p, _ in self.published if t == "nodes/rpi/data_ack"]


@pytest.fixture(name="inbox")
def inbox_fixture(tmp_path: Path) -> Inbox:
    return Inbox(tmp_path / "inbox", 10 * 1024 * 1024)


@pytest.fixture(name="host")
def host_fixture(inbox: Inbox) -> _Host:
    return _Host(inbox)


@pytest.fixture(name="receiver")
def receiver_fixture(host: _Host) -> DataFiles:
    # The fake declares what the receiver reads; it stands in for MainActor.
    return DataFiles(cast("ArtifactHost", host))


async def _deliver(inbox: Inbox, published: _Published) -> None:
    """Hand the inbox what the node published on the blob path, as main's listener does."""
    for topic, data in published.messages:
        sha = sha256_of_topic(topic)
        if sha is not None:
            await inbox.accept(sha, data)


class TestMainFiles:
    async def test_the_file_lands_under_its_source_and_hour_and_is_answered(
        self, tmp_path: Path, host: _Host, inbox: Inbox, receiver: DataFiles, published: _Published
    ) -> None:
        shipper = DataShipper("rpi", tmp_path / "node", published)
        _recorded(shipper.root, "sensors/imu/left")
        await shipper.look()
        (notice,) = published.notices()
        await _deliver(inbox, published)

        kept = await receiver.take("rpi", notice)

        assert kept
        root = data_root(resolve_state_dir())
        assert frame(root, "sensors/imu/left", T0, T0 + 10).column("x").to_pylist() == [0.0, 1.0]
        ((node, placed),) = receiver.filed
        assert (node, placed) == ("rpi", root / "sensors/imu/left/2025/10/09/08.parquet")
        (ack,) = host.acks()
        assert (ack["sha256"], ack["ok"]) == (notice["sha256"], True)
        assert not inbox.path(notice["sha256"]).exists()

    async def test_mains_own_hour_is_kept_and_the_arrival_becomes_a_part(
        self, tmp_path: Path, host: _Host, inbox: Inbox, receiver: DataFiles, published: _Published
    ) -> None:
        root = data_root(resolve_state_dir())
        _recorded(root, "sensors/imu/left", 1)
        shipper = DataShipper("rpi", tmp_path / "node", published)
        _recorded(shipper.root, "sensors/imu/left")
        await shipper.look()
        (notice,) = published.notices()
        await _deliver(inbox, published)

        await receiver.take("rpi", notice)

        names = sorted(p.name for p in (root / "sensors/imu/left/2025/10/09").iterdir())
        assert names == ["08-0001.parquet", "08.parquet"]
        assert frame(root, "sensors/imu/left", T0, T0 + 10).num_rows == 3

    async def test_a_file_that_never_arrives_is_answered_no(
        self, host: _Host, receiver: DataFiles, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import wactorz.agents.main.data_files as module

        monkeypatch.setattr(module, "wait_for", lambda size: 0.05)

        kept = await receiver.take(
            "rpi",
            {"source": "s", "path": "s/2025/10/09/08.parquet", "sha256": "a" * 64, "size": 10},
        )

        assert not kept
        (ack,) = host.acks()
        assert ack["ok"] is False
        assert "did not arrive" in ack["reason"]

    @pytest.mark.parametrize(
        ("notice", "words"),
        [
            ({"source": "s"}, "names source, path"),
            (
                {
                    "source": "s",
                    "path": "other/2025/10/09/08.parquet",
                    "sha256": "a" * 64,
                    "size": 1,
                },
                "not an hour of",
            ),
            (
                {"source": "s", "path": "s/../../x/08.parquet", "sha256": "a" * 64, "size": 1},
                "not an hour of",
            ),
            (
                {
                    "source": "../s",
                    "path": "../s/2025/10/09/08.parquet",
                    "sha256": "a" * 64,
                    "size": 1,
                },
                "cannot name a source",
            ),
            (
                {
                    "source": "s",
                    "path": "s/2025/10/09/08.parquet",
                    "sha256": "a" * 64,
                    "size": 10**9,
                },
                "over the",
            ),
        ],
    )
    async def test_a_notice_that_cannot_place_a_file_is_refused(
        self, host: _Host, receiver: DataFiles, notice: dict[str, Any], words: str
    ) -> None:
        kept = await receiver.take("rpi", notice)

        assert not kept
        (ack,) = host.acks()
        assert ack["ok"] is False
        assert words in ack["reason"]

    async def test_a_notice_is_taken_off_the_listeners_loop(
        self, host: _Host, receiver: DataFiles
    ) -> None:
        await receiver.receive("nodes/rpi/data_file", json.dumps({"source": "s"}).encode())
        for _ in range(50):
            if host.acks():
                break
            await asyncio.sleep(0.01)

        assert host.acks()[0]["ok"] is False
        await receiver.receive("nodes/rpi/other", b"{}")
        await receiver.receive("nodes/rpi/data_file", b"not json")
        assert len(host.acks()) == 1


class TestPlacement:
    def test_a_path_is_its_source_then_a_year_month_day_and_hour(self, tmp_path: Path) -> None:
        assert placement(tmp_path, "a/b", "a/b/2025/10/09/08-0002.parquet") == (
            tmp_path / "a/b/2025/10/09/08-0002.parquet"
        )
        with pytest.raises(DataFileError):
            placement(tmp_path, "a/b", "a/b/2025/10/09/readme.txt")
        with pytest.raises(DataFileError):
            placement(tmp_path, "a/b", "a/b/2025/10/08.parquet")
        with pytest.raises(DataError):
            placement(tmp_path, "a//b", "a//b/2025/10/09/08.parquet")

    def test_the_next_free_name_is_a_further_part(self, tmp_path: Path) -> None:
        target = tmp_path / "08.parquet"
        assert next_free(target) == target
        target.write_bytes(b"x")
        assert next_free(target) == tmp_path / "08-0001.parquet"
        (tmp_path / "08-0001.parquet").write_bytes(b"x")
        assert next_free(target) == tmp_path / "08-0002.parquet"
