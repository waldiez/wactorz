"""The model routes: reading the store, and moving its pointers from the dashboard.

Each move is the store's own, recorded as made by the dashboard, and refused in
the store's words: a 409 for a move the pointers do not allow now, a 404 for a
name or version that is not stored, a 400 for a request that is not one.
"""

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from wactorz.core.artifacts import ArtifactStore
from wactorz.web import api_models
from wactorz.web.app import build_app


@pytest.fixture(name="store")
def store_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ArtifactStore:
    monkeypatch.setattr(api_models, "resolve_state_dir", lambda: tmp_path)
    store = ArtifactStore.in_state_dir(tmp_path)
    for n in range(1, 4):
        weights = tmp_path / f"weights-{n}.bin"
        weights.write_bytes(bytes([n]) * 8)
        store.add(weights, "imu", format_name="raw", metadata={"caught": n / 4})
    return store


@pytest.fixture(name="client")
async def client_fixture(store: ArtifactStore) -> AsyncIterator[TestClient]:
    client = TestClient(TestServer(build_app()))
    await client.start_server()
    yield client
    await client.close()


async def test_the_list_gives_each_name_its_newest_version_and_pointers(
    client: TestClient, store: ArtifactStore
) -> None:
    store.move("active", "imu:2", by="cli")

    response = await client.get("/api/models")

    assert response.status == 200
    (imu,) = (await response.json())["models"]
    assert (imu["name"], imu["newest"]["version"], imu["versions"]) == ("imu", 3, 3)
    assert imu["pointers"]["active"]["version"] == 2
    assert imu["pointers"]["active"]["holds"] is True


async def test_one_model_gives_every_version_with_what_was_said_of_it(client: TestClient) -> None:
    response = await client.get("/api/models/imu")

    assert response.status == 200
    body = await response.json()
    assert [v["metadata"]["caught"] for v in body["versions"]] == [0.25, 0.5, 0.75]
    assert body["pointers"] == {}


async def test_a_model_that_is_not_stored_is_a_404(client: TestClient) -> None:
    response = await client.get("/api/models/nothing-here")

    assert response.status == 404


async def test_setting_active_records_the_dashboard_and_the_reason(
    client: TestClient, store: ArtifactStore
) -> None:
    response = await client.post(
        "/api/models/imu/active", json={"version": 2, "reason": "  better recall  "}
    )

    assert response.status == 200
    pointer = (await response.json())["pointer"]
    assert (pointer["version"], pointer["by"], pointer["reason"]) == (
        2,
        "dashboard",
        "better recall",
    )
    assert store.pointer("imu", "active") is not None


async def test_a_shadow_then_promoted_becomes_active_and_ends_the_trial(
    client: TestClient, store: ArtifactStore
) -> None:
    await client.post("/api/models/imu/active", json={"version": 1})
    await client.post("/api/models/imu/shadow", json={"version": 3})

    response = await client.post("/api/models/imu/promote", json={"reason": "held up"})

    assert response.status == 200
    assert (await response.json())["pointer"]["version"] == 3
    assert store.pointer("imu", "shadow") is None


async def test_rolling_back_returns_active_to_the_version_it_left(
    client: TestClient, store: ArtifactStore
) -> None:
    await client.post("/api/models/imu/active", json={"version": 1})
    await client.post("/api/models/imu/active", json={"version": 3})

    response = await client.post("/api/models/imu/rollback")

    assert response.status == 200
    pointer = (await response.json())["pointer"]
    assert (pointer["version"], pointer["previous"]) == (1, 3)


async def test_clearing_the_shadow_ends_the_trial(client: TestClient, store: ArtifactStore) -> None:
    await client.post("/api/models/imu/shadow", json={"version": 2})

    response = await client.delete("/api/models/imu/shadow")

    assert response.status == 200
    assert store.pointer("imu", "shadow") is None


async def test_a_clear_keeps_its_reason(client: TestClient, store: ArtifactStore) -> None:
    await client.post("/api/models/imu/shadow", json={"version": 2})

    response = await client.delete("/api/models/imu/shadow", json={"reason": " too noisy "})

    assert response.status == 200
    cleared = store.pointer_changes()[0][-1]
    assert (cleared["change"], cleared["by"], cleared["reason"]) == (
        "cleared",
        "dashboard",
        "too noisy",
    )


async def test_a_clear_with_a_reason_that_is_not_text_is_refused(
    client: TestClient, store: ArtifactStore
) -> None:
    await client.post("/api/models/imu/shadow", json={"version": 2})

    response = await client.delete("/api/models/imu/shadow", json={"reason": 3})

    assert response.status == 400
    assert store.pointer("imu", "shadow") is not None


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/api/models/imu/rollback", None),
        ("post", "/api/models/imu/promote", None),
        ("delete", "/api/models/imu/shadow", None),
    ],
)
async def test_a_move_the_pointers_do_not_allow_now_is_a_409(
    client: TestClient, method: str, path: str, body: dict | None
) -> None:
    response = await getattr(client, method)(path, json=body)

    assert response.status == 409
    assert (await response.json())["error"]


async def test_setting_active_where_it_already_is_is_a_409(client: TestClient) -> None:
    await client.post("/api/models/imu/active", json={"version": 2})

    response = await client.post("/api/models/imu/active", json={"version": 2})

    assert response.status == 409
    assert (await response.json())["error"] == "imu:2 is already active"


async def test_rolling_back_the_first_move_has_nowhere_to_go(client: TestClient) -> None:
    await client.post("/api/models/imu/active", json={"version": 2})

    response = await client.post("/api/models/imu/rollback")

    assert response.status == 409
    assert "no earlier one" in (await response.json())["error"]


async def test_a_version_that_is_not_stored_is_a_404(client: TestClient) -> None:
    response = await client.post("/api/models/imu/active", json={"version": 9})

    assert response.status == 404


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/models/imu/active", {}),
        ("/api/models/imu/active", {"version": "2"}),
        ("/api/models/imu/active", {"version": True}),
        ("/api/models/imu/active", {"version": 0}),
        ("/api/models/imu/shadow", {"version": 2, "reason": 7}),
        ("/api/models/imu/active", [2]),
        ("/api/models/..bad/active", {"version": 2}),
    ],
)
async def test_a_request_that_is_not_one_is_a_400(
    client: TestClient, path: str, body: object
) -> None:
    response = await client.post(path, json=body)

    assert response.status == 400


async def test_a_body_that_is_not_json_is_a_400(client: TestClient) -> None:
    response = await client.post(
        "/api/models/imu/active", data=b"{nope", headers={"Content-Type": "application/json"}
    )

    assert response.status == 400


async def test_a_long_reason_is_kept_to_a_sentence(client: TestClient) -> None:
    response = await client.post(
        "/api/models/imu/active", json={"version": 2, "reason": "x" * 2000}
    )

    assert len((await response.json())["pointer"]["reason"]) == api_models.MAX_REASON_CHARS


async def test_a_pointer_whose_version_holds_other_bytes_says_so(
    client: TestClient, store: ArtifactStore
) -> None:
    store.move("shadow", "imu:3", by="cli")
    # The bytes the pointer was set to, no longer the ones imu:3 holds: what an
    # index says once a removed version's number is reused.
    index = json.loads(store.index_path.read_text(encoding="utf-8"))
    index["pointers"]["imu"]["shadow"]["sha256"] = "0" * 64
    store.index_path.write_text(json.dumps(index), encoding="utf-8")

    response = await client.get("/api/models/imu")

    assert (await response.json())["pointers"]["shadow"]["holds"] is False
