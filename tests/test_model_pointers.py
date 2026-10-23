"""The two pointers on the model store: ``active``, the version agents serve, and ``shadow``, one on trial.

They live in the store's index beside the versions, and nothing changes for a
store without them: a bare name means the newest until ``active`` is set. A
move is its own act, recorded with who and why; a version a pointer names
cannot be removed; a spawn that followed a pointer follows it when pinned
again; main says where every pointer stands on a retained topic per pointer,
and on the feed when one moves. ``docs/design/model-pointers.md`` is the
decision these pin.
"""

import json
import logging
import re
from pathlib import Path
from typing import Any, cast

import pytest

from wactorz import artifacts_cli
from wactorz.agents.main import artifact_service as service_module
from wactorz.agents.main.actor import MainActor
from wactorz.agents.main.artifact_service import ArtifactService, pointer_topic
from wactorz.agents.main.commands import models as models_commands
from wactorz.agents.main.commands import registry
from wactorz.agents.main.commands.dispatch import CommandContext
from wactorz.agents.main.hosts import ArtifactHost
from wactorz.core.actor import ActorState
from wactorz.core.artifacts import (
    INDEX_FORMAT,
    ArtifactError,
    ArtifactNotFoundError,
    ArtifactStore,
    Pointer,
    check_pinned,
    pinned,
    summary,
)


@pytest.fixture(name="state")
def state_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Main's state directory, where the service and the chat verbs look for the store."""
    state = tmp_path / "state"
    monkeypatch.setattr(service_module, "resolve_state_dir", lambda: str(state))
    monkeypatch.setattr(models_commands, "resolve_state_dir", lambda: str(state))
    return state


@pytest.fixture(name="store")
def store_fixture(state: Path) -> ArtifactStore:
    return ArtifactStore.in_state_dir(state)


def add(store: ArtifactStore, tmp_path: Path, name: str, count: int = 1) -> None:
    """``count`` more versions of ``name``, each of different bytes."""
    for _ in range(count):
        version = len(store.versions(name)) + 1
        path = tmp_path / f"{name}-{version}.onnx"
        path.write_bytes(f"{name} weights {version}".encode())
        store.add(path, name)


@pytest.fixture(name="imu")
def imu_fixture(store: ArtifactStore, tmp_path: Path) -> ArtifactStore:
    """``imu`` in three versions, no pointer set."""
    add(store, tmp_path, "imu", 3)
    return store


# ── Resolving ─────────────────────────────────────────────────────────────────


class TestResolving:
    def test_a_bare_name_is_the_newest_until_active_is_set(self, imu: ArtifactStore) -> None:
        assert imu.resolve("imu").version == 3

        imu.move("active", "imu:2", by="test")

        assert imu.resolve("imu").version == 2

    def test_latest_is_the_newest_whatever_the_pointers_say(self, imu: ArtifactStore) -> None:
        imu.move("active", "imu:1", by="test")

        assert imu.resolve("imu:latest").version == 3

    def test_the_pointers_can_be_named(self, imu: ArtifactStore) -> None:
        imu.move("active", "imu:2", by="test")
        imu.move("shadow", "imu:3", by="test")

        assert imu.resolve("imu:active").version == 2
        assert imu.resolve("imu:shadow").version == 3

    @pytest.mark.parametrize("which", ["active", "shadow"])
    def test_a_pointer_that_is_not_set_is_an_error(self, imu: ArtifactStore, which: str) -> None:
        with pytest.raises(ArtifactError, match=f"imu has no {which} version"):
            imu.resolve(f"imu:{which}")

    def test_a_pointer_whose_version_holds_other_bytes_is_reported(
        self, imu: ArtifactStore
    ) -> None:
        imu.move("active", "imu:2", by="test")
        body = json.loads(imu.index_path.read_text())
        body["pointers"]["imu"]["active"]["sha256"] = "0" * 64
        imu.index_path.write_text(json.dumps(body))

        with pytest.raises(ArtifactError, match="no longer holds the bytes"):
            imu.resolve("imu")

    def test_reading_a_pointer_that_is_not_one_is_refused(self, imu: ArtifactStore) -> None:
        with pytest.raises(ArtifactError, match="not a pointer"):
            imu.pointer("imu", "canary")


# ── Moving ────────────────────────────────────────────────────────────────────


class TestMoving:
    def test_a_move_says_where_from_who_and_why(self, imu: ArtifactStore) -> None:
        first = imu.move("active", "imu:1", by="cli")
        second = imu.move("active", "imu:latest", by="agent:controller", reason="auc 0.95")

        assert (first.version, first.previous, first.by, first.reason) == (1, None, "cli", None)
        assert (second.version, second.previous) == (3, 1)
        assert (second.by, second.reason) == ("agent:controller", "auc 0.95")
        assert second.moved_at >= first.moved_at
        assert imu.pointer("imu", "active") == second
        assert imu.pointers("imu") == {"active": second}

    @pytest.mark.parametrize(
        ("which", "reference"),
        [("active", "imu"), ("active", "imu:active"), ("shadow", "imu:shadow")],
    )
    def test_the_pointer_itself_is_not_a_place_to_move_to(
        self, imu: ArtifactStore, which: str, reference: str
    ) -> None:
        with pytest.raises(ArtifactError, match=r"would mean the .* pointer itself"):
            imu.move(which, reference, by="test")

    def test_a_move_to_where_it_stands_is_refused(self, imu: ArtifactStore) -> None:
        imu.move("active", "imu:2", by="test")

        with pytest.raises(ArtifactError, match="already active"):
            imu.move("active", "imu:2", by="test")

    def test_a_shadow_of_the_active_version_is_refused(self, imu: ArtifactStore) -> None:
        imu.move("active", "imu:2", by="test")

        with pytest.raises(ArtifactError, match="is active; a shadow is a different version"):
            imu.move("shadow", "imu:2", by="test")

    def test_moving_active_onto_the_shadow_is_a_promotion(self, imu: ArtifactStore) -> None:
        imu.move("active", "imu:1", by="test")
        imu.move("shadow", "imu:3", by="test")

        moved = imu.move("active", "imu:shadow", by="test")

        assert (moved.version, moved.previous) == (3, 1)
        assert imu.pointer("imu", "shadow") is None

    def test_promote_moves_active_to_the_shadow_and_clears_it(self, imu: ArtifactStore) -> None:
        imu.move("active", "imu:1", by="test")
        imu.move("shadow", "imu:2", by="test")

        promoted = imu.promote("imu", by="cli", reason="a day beside the old one")

        assert (promoted.pointer, promoted.version, promoted.previous) == ("active", 2, 1)
        assert promoted.reason == "a day beside the old one"
        assert imu.pointers("imu") == {"active": promoted}

    def test_promote_without_a_shadow_is_refused(self, imu: ArtifactStore) -> None:
        with pytest.raises(ArtifactError, match="no shadow version to promote"):
            imu.promote("imu", by="test")

    def test_clear_unsets_a_pointer_and_says_what_it_was(self, imu: ArtifactStore) -> None:
        imu.move("active", "imu:1", by="test")

        was = imu.clear("active", "imu")

        assert (was.pointer, was.version) == ("active", 1)
        assert imu.pointers("imu") == {}
        assert imu.resolve("imu").version == 3, "the bare name means the newest again"

    def test_clearing_what_is_not_set_is_refused(self, imu: ArtifactStore) -> None:
        with pytest.raises(ArtifactError, match="imu has no shadow version"):
            imu.clear("shadow", "imu")

    def test_a_pointer_that_is_not_one_is_refused(self, imu: ArtifactStore) -> None:
        with pytest.raises(ArtifactError, match="not a pointer"):
            imu.move("canary", "imu:1", by="test")

    def test_a_version_that_is_not_there_cannot_be_pointed_at(self, imu: ArtifactStore) -> None:
        with pytest.raises(ArtifactError):
            imu.move("active", "imu:9", by="test")
        with pytest.raises(ArtifactError):
            imu.move("active", "other:1", by="test")

    @pytest.mark.parametrize("name", ["bad name", "imu:1"])
    def test_a_name_that_is_not_one_is_refused_everywhere(
        self, imu: ArtifactStore, name: str
    ) -> None:
        with pytest.raises(ArtifactError, match="not a usable name"):
            imu.pointers(name)
        with pytest.raises(ArtifactError, match="not a usable name"):
            imu.clear("active", name)
        with pytest.raises(ArtifactError, match="not a usable name"):
            imu.promote(name, by="test")


# ── Removing ──────────────────────────────────────────────────────────────────


class TestRemoving:
    def test_a_version_a_pointer_names_stays_until_the_pointer_moves(
        self, imu: ArtifactStore
    ) -> None:
        imu.move("shadow", "imu:2", by="test")

        with pytest.raises(ArtifactError, match="imu:2 is the shadow version; move the pointer"):
            imu.remove("imu:2")
        with pytest.raises(ArtifactError, match="shadow"):
            imu.remove("imu:shadow")

        imu.clear("shadow", "imu")
        assert imu.remove("imu:2")[0].version == 2

    def test_removing_the_bare_name_removes_the_newest_not_the_active(
        self, imu: ArtifactStore
    ) -> None:
        imu.move("active", "imu:1", by="test")

        (removed,) = imu.remove("imu:latest")

        assert removed.version == 3
        assert imu.pointer("imu", "active") is not None

    def test_a_name_that_is_not_stored_is_not_found(self, imu: ArtifactStore) -> None:
        with pytest.raises(ArtifactNotFoundError):
            imu.remove("other:1")

    def test_every_version_takes_the_pointers_with_it(self, imu: ArtifactStore) -> None:
        imu.move("active", "imu:1", by="test")
        imu.move("shadow", "imu:2", by="test")

        imu.remove("imu", every_version=True)

        assert imu.pointers("imu") == {}
        assert "pointers" in json.loads(imu.index_path.read_text())
        assert json.loads(imu.index_path.read_text())["pointers"] == {}


# ── The index ─────────────────────────────────────────────────────────────────


class TestTheIndex:
    def test_pointers_are_kept_beside_the_versions(self, imu: ArtifactStore) -> None:
        imu.move("active", "imu:2", by="cli", reason="why")

        body = json.loads(imu.index_path.read_text())

        assert body["format"] == INDEX_FORMAT == 2
        entry = body["pointers"]["imu"]["active"]
        assert entry["version"] == 2 and entry["sha256"] == imu.resolve("imu:2").sha256
        assert (entry["previous"], entry["by"], entry["reason"]) == (None, "cli", "why")
        assert "moved_at" in entry

    def test_a_name_without_pointers_is_not_listed_among_them(
        self, imu: ArtifactStore, tmp_path: Path
    ) -> None:
        add(imu, tmp_path, "other")
        imu.move("active", "imu:1", by="test")

        assert list(json.loads(imu.index_path.read_text())["pointers"]) == ["imu"]

    def test_an_index_without_pointers_reads_as_having_none_and_is_written_back_with_them(
        self, imu: ArtifactStore
    ) -> None:
        body = json.loads(imu.index_path.read_text())
        del body["pointers"]
        body["format"] = 1
        imu.index_path.write_text(json.dumps(body))

        assert imu.pointers("imu") == {}
        assert imu.resolve("imu").version == 3

        imu.move("active", "imu:1", by="test")
        assert json.loads(imu.index_path.read_text())["format"] == 2


# ── Pinning ───────────────────────────────────────────────────────────────────


class TestPinning:
    def test_a_bare_name_and_the_pointers_follow_what_they_named(self, imu: ArtifactStore) -> None:
        imu.move("active", "imu:1", by="test")
        imu.move("shadow", "imu:2", by="test")

        assert imu.pin("imu")["follows"] == "active"
        assert imu.pin("imu:active")["follows"] == "active"
        assert imu.pin("imu:shadow") == {**pinned(imu.resolve("imu:2")), "follows": "shadow"}
        assert "follows" not in imu.pin("imu:3")
        assert "follows" not in imu.pin("imu:latest")

    def test_pinned_again_a_follower_goes_to_where_the_pointer_is_now(
        self, imu: ArtifactStore
    ) -> None:
        imu.move("active", "imu:1", by="test")
        before = imu.pin("imu")

        imu.move("active", "imu:3", by="test")

        again = imu.pin(before)
        assert again["version"] == 3 and again["follows"] == "active"

    def test_with_no_pointer_to_follow_it_keeps_the_version_it_ran(
        self, imu: ArtifactStore
    ) -> None:
        before = imu.pin("imu")
        assert before["version"] == 3

        imu.remove("imu:3")
        with pytest.raises(ArtifactError):
            imu.pin(before)

        add(imu, Path(imu.root.parent.parent), "imu")
        kept = imu.pin({**before, "version": 2, "sha256": imu.resolve("imu:2").sha256})
        assert kept["version"] == 2 and kept["follows"] == "active"

    def test_one_that_named_a_version_stays_on_it_through_a_move(self, imu: ArtifactStore) -> None:
        before = imu.pin("imu:1")
        imu.move("active", "imu:3", by="test")

        assert imu.pin(before) == before

    def test_following_something_that_is_not_a_pointer_is_refused(self, imu: ArtifactStore) -> None:
        with pytest.raises(ArtifactError, match="not a pointer"):
            check_pinned({**pinned(imu.resolve("imu:1")), "follows": "newest"})


# ── What main and the planner are told ────────────────────────────────────────


def test_the_summary_says_which_versions_are_active_and_shadow(imu: ArtifactStore) -> None:
    imu.move("active", "imu:2", by="test")
    imu.move("shadow", "imu:3", by="test")

    (line,) = summary(imu)

    assert line.startswith(
        "imu — newest imu:3 (onnx, 13 B), 3 versions, active imu:2, shadow imu:3"
    )


# ── wactorz-artifacts ─────────────────────────────────────────────────────────


class TestTheCommand:
    @pytest.fixture(autouse=True)
    def _capture(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="wactorz.artifacts_cli")
        self.caplog = caplog

    def run(self, *argv: str, state: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
        self.caplog.clear()
        code = artifacts_cli.main([*argv, "--state-dir", str(state)])
        return code, capsys.readouterr().out + self.caplog.text

    def test_activate_shadow_promote_and_clear(
        self, imu: ArtifactStore, state: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out = self.run(
            "activate", "imu:1", "--reason", "known good", state=state, capsys=capsys
        )
        assert code == 0 and "imu:1 is now active" in out
        active = imu.pointer("imu", "active")
        assert active is not None
        assert active == Pointer(
            "imu",
            "active",
            1,
            imu.resolve("imu:1").sha256,
            None,
            active.moved_at,
            "cli",
            "known good",
        )

        code, out = self.run("shadow", "imu:3", state=state, capsys=capsys)
        assert code == 0 and "imu:3 is now shadow" in out

        code, out = self.run("list", state=state, capsys=capsys)
        assert code == 0
        lines = {
            found.group(1): line
            for line in out.splitlines()
            if (found := re.search(r"\b(imu:\d)\s", line))
        }
        assert lines["imu:1"].endswith("active") and lines["imu:3"].endswith("shadow")
        assert not lines["imu:2"].endswith(("active", "shadow"))

        code, out = self.run("promote", "imu", state=state, capsys=capsys)
        assert code == 0 and "imu:3 is now active (was imu:1)" in out

        code, out = self.run("clear", "active", "imu", state=state, capsys=capsys)
        assert code == 0 and "imu has no active version now (was imu:3)" in out
        assert imu.pointers("imu") == {}

    def test_show_includes_the_pointers(
        self, imu: ArtifactStore, state: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        imu.move("active", "imu:2", by="cli")

        assert artifacts_cli.main(["show", "imu", "--state-dir", str(state)]) == 0
        shown = json.loads(capsys.readouterr().out)

        assert shown["reference"] == "imu:2", "the bare name is the active version"
        assert shown["pointers"]["active"]["version"] == 2
        assert shown["pointers"]["active"]["by"] == "cli"

    def test_a_refusal_is_said_and_fails(
        self, imu: ArtifactStore, state: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        imu.move("active", "imu:2", by="cli")

        code, out = self.run("shadow", "imu:2", state=state, capsys=capsys)
        assert code == 1 and "a shadow is a different version" in out

        code, out = self.run("remove", "imu:2", state=state, capsys=capsys)
        assert code == 1 and "move the pointer first" in out

        code, out = self.run("activate", "imu", state=state, capsys=capsys)
        assert code == 1 and "pointer itself" in out


# ── Main saying where they stand ──────────────────────────────────────────────


class _Host:
    """Main, as the pointer watcher sees it: a name, an id, a state and a broker."""

    def __init__(self) -> None:
        self.name = "main"
        self.actor_id = "main-id"
        self.state = ActorState.RUNNING
        self._mqtt_broker = "localhost"
        self._mqtt_port = 1883
        self.published: list[tuple[str, Any, bool, int]] = []

    def _get_spawn_registry(self) -> dict[str, dict[str, Any]]:
        return {}

    async def _mqtt_publish(
        self, topic: str, payload: Any, retain: bool = False, qos: int = 0
    ) -> None:
        self.published.append((topic, payload, retain, qos))

    def on(self, topic: str) -> list[Any]:
        return [payload for t, payload, _, _ in self.published if t == topic]

    def feed(self) -> list[str]:
        return [
            payload["message"]
            for t, payload, _, _ in self.published
            if t == "agents/main-id/logs" and payload.get("type") == "model"
        ]


@pytest.fixture(name="host")
def host_fixture() -> _Host:
    return _Host()


@pytest.fixture(name="service")
def service_fixture(host: _Host) -> ArtifactService:
    # The fake declares what the service reads; it stands in for MainActor.
    return ArtifactService(cast("ArtifactHost", host))


class TestPublishing:
    async def test_the_first_look_says_where_every_pointer_stands(
        self, imu: ArtifactStore, tmp_path: Path, host: _Host, service: ArtifactService
    ) -> None:
        add(imu, tmp_path, "yolo")
        imu.move("active", "imu:2", by="cli", reason="why")

        await service.publish_pointers()

        (active,) = host.on(pointer_topic("imu", "active"))
        assert active["version"] == 2 and active["by"] == "cli" and active["reason"] == "why"
        assert active["name"] == "imu" and active["pointer"] == "active"
        assert host.on(pointer_topic("imu", "shadow")) == [b""]
        assert host.on(pointer_topic("yolo", "active")) == [b""]
        assert host.on(pointer_topic("yolo", "shadow")) == [b""]
        assert all(
            retain and qos == 1 for t, _, retain, qos in host.published if t.startswith("models/")
        )
        assert host.feed() == [], "nothing moved; main only came back"

    async def test_a_move_found_later_is_published_once_and_said_on_the_feed(
        self, imu: ArtifactStore, host: _Host, service: ArtifactService
    ) -> None:
        await service.publish_pointers()
        host.published.clear()

        await service.publish_pointers()
        assert host.published == [], "nothing changed, nothing said"

        imu.move("active", "imu:3", by="cli", reason="auc 0.95")
        await service.publish_pointers()

        (active,) = host.on(pointer_topic("imu", "active"))
        assert active["version"] == 3
        assert host.feed() == ["imu:3 is now active, by cli: auc 0.95"]

    async def test_a_model_just_stored_is_published_and_not_said(
        self, imu: ArtifactStore, tmp_path: Path, host: _Host, service: ArtifactService
    ) -> None:
        await service.publish_pointers()
        host.published.clear()

        add(imu, tmp_path, "yolo")
        await service.publish_pointers()

        assert host.on(pointer_topic("yolo", "active")) == [b""]
        assert host.on(pointer_topic("yolo", "shadow")) == [b""]
        assert host.feed() == [], "nothing moved"

    async def test_a_clear_and_a_removed_name_publish_empties(
        self, imu: ArtifactStore, host: _Host, service: ArtifactService
    ) -> None:
        imu.move("active", "imu:1", by="cli")
        imu.move("shadow", "imu:2", by="cli")
        await service.publish_pointers()
        host.published.clear()

        imu.clear("shadow", "imu")
        await service.publish_pointers()
        assert host.on(pointer_topic("imu", "shadow")) == [b""]
        assert host.feed() == ["imu has no shadow version now (was imu:2)"]

        host.published.clear()
        imu.remove("imu", every_version=True)
        await service.publish_pointers()
        assert host.on(pointer_topic("imu", "active")) == [b""]
        assert host.on(pointer_topic("imu", "shadow")) == []
        assert host.feed() == ["imu has no active version now (was imu:1)"]

    async def test_a_promotion_says_both_moves(
        self, imu: ArtifactStore, host: _Host, service: ArtifactService
    ) -> None:
        imu.move("active", "imu:1", by="cli")
        imu.move("shadow", "imu:2", by="cli")
        await service.publish_pointers()
        host.published.clear()

        imu.promote("imu", by="chat")
        await service.publish_pointers()

        assert host.feed() == [
            "imu:2 is now active (was imu:1), by chat",
            "imu has no shadow version now (was imu:2)",
        ]

    async def test_the_watcher_looks_until_the_actor_stops(
        self,
        imu: ArtifactStore,
        host: _Host,
        service: ArtifactService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(service_module, "POINTER_POLL_S", 0.01)
        looks = 0

        async def counting() -> None:
            nonlocal looks
            looks += 1
            if looks == 3:
                host.state = ActorState.STOPPED

        monkeypatch.setattr(service, "publish_pointers", counting)

        await service.pointer_watcher()

        assert looks == 3

    async def test_a_store_it_cannot_read_is_said_once(
        self,
        imu: ArtifactStore,
        host: _Host,
        service: ArtifactService,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(service_module, "POINTER_POLL_S", 0.01)
        imu.index_path.write_text("{not json")
        looks = 0
        real = service.publish_pointers

        async def failing() -> None:
            nonlocal looks
            looks += 1
            if looks == 3:
                host.state = ActorState.STOPPED
            await real()

        monkeypatch.setattr(service, "publish_pointers", failing)

        with caplog.at_level(logging.WARNING, logger="wactorz.agents.main.artifact_service"):
            await service.pointer_watcher()

        assert looks == 3
        assert (
            len([r for r in caplog.records if "cannot publish the model pointers" in r.message])
            == 1
        )


# ── /artifacts activate | shadow | promote | clear ────────────────────────────


class _Announcing:
    def __init__(self) -> None:
        self.announced = 0

    async def publish_pointers(self) -> None:
        self.announced += 1


class _Actor:
    def __init__(self) -> None:
        self.artifact_service = _Announcing()


def ctx() -> tuple[CommandContext, _Actor]:
    actor = _Actor()
    # The verbs reach only the artifact service; the fake stands in for MainActor.
    return CommandContext(actor=cast(MainActor, actor)), actor


class TestTheChatVerbs:
    async def test_activate_moves_the_pointer_with_the_rest_as_the_reason(
        self, imu: ArtifactStore
    ) -> None:
        context, actor = ctx()

        reply = await models_commands.activate_model(context, "imu:2 it scored better")

        assert reply == "imu:2 is now active."
        moved = imu.pointer("imu", "active")
        assert moved is not None and (moved.by, moved.reason) == ("chat", "it scored better")
        assert actor.artifact_service.announced == 1, "heard at once, not at the next look"

    async def test_shadow_promote_and_clear(self, imu: ArtifactStore) -> None:
        context, actor = ctx()
        await models_commands.activate_model(context, "imu:1")

        assert await models_commands.shadow_model(context, "imu:3") == "imu:3 is now shadow."
        assert await models_commands.promote_model(context, "imu a day beside") == (
            "imu:3 is now active (was imu:1)."
        )
        assert imu.pointer("imu", "active").reason == "a day beside"  # pyright: ignore[reportOptionalMemberAccess]
        assert await models_commands.clear_model_pointer(context, "active imu") == (
            "imu has no active version now (was imu:3)."
        )
        assert imu.pointers("imu") == {}
        assert actor.artifact_service.announced == 4

    async def test_a_refusal_is_the_stores_reason(self, imu: ArtifactStore) -> None:
        context, actor = ctx()

        assert (await models_commands.activate_model(context, "imu")).startswith(
            "Cannot activate 'imu': 'imu' would mean the active pointer itself"
        )
        assert (await models_commands.promote_model(context, "imu")).startswith(
            "Cannot promote 'imu': imu has no shadow version"
        )
        await models_commands.activate_model(context, "imu:2")
        assert (await models_commands.shadow_model(context, "imu:2")).startswith(
            "Cannot shadow 'imu:2': imu:2 is active"
        )
        assert (await models_commands.clear_model_pointer(context, "active")).startswith("Usage:")
        assert (await models_commands.clear_model_pointer(context, "shadow imu")).startswith(
            "Cannot clear 'shadow' of 'imu': imu has no shadow version"
        )
        assert actor.artifact_service.announced == 1, "only the move that happened"

    @pytest.mark.parametrize(
        ("typed", "handler"),
        [
            ("/artifacts activate imu:2", models_commands.activate_model),
            ("/artifacts shadow imu:2", models_commands.shadow_model),
            ("/artifacts promote imu", models_commands.promote_model),
            ("/artifacts clear active imu", models_commands.clear_model_pointer),
        ],
    )
    def test_each_verb_is_found_ahead_of_the_listing(self, typed: str, handler: Any) -> None:
        found = registry.find(typed)

        assert found is not None and found[0].handler is handler

    @pytest.mark.parametrize("typed", ["/artifacts", "/artifacts imu", "/artifacts activate"])
    def test_the_listing_still_answers_a_name(self, typed: str) -> None:
        found = registry.find(typed)

        assert found is not None and found[0].name == "/artifacts"

    def test_a_social_channel_may_not_move_them(self) -> None:
        for typed in ("/artifacts activate imu:2", "/artifacts clear active imu"):
            found = registry.find(typed)
            assert found is not None and not found[0].restricted_ok
