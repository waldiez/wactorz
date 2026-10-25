"""A library agent's code goes with it to a node that does not have it.

Main sends the folder its module is in, ``.py`` files only, and refuses a folder
that imports this machine's own code from outside it, naming the import. The
node checks the hash, writes the files where its bundles are kept, and builds
the agent from them, whatever it has installed.
"""

import importlib
import json
import sys
import textwrap
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.test_migrate_agent import _Main as MigratingMain
from tests.test_migrate_agent import online
from tests.test_node_runner import RecordingRunner
from tests.test_spawn_remote import _Main as SpawningMain
from wactorz import plugins
from wactorz.core import bundles
from wactorz.core.bundles import BundleError, bundle_for, checked, digest, prepare_import, write


class Folder:
    """An agent's folder on disk, importable under names no other test uses."""

    def __init__(self, root: Path) -> None:
        self.root = root
        suffix = uuid.uuid4().hex[:8]
        self.agent = f"agent_{suffix}"
        self.helper = f"helper_{suffix}"
        root.mkdir(parents=True)

    @property
    def target(self) -> str:
        return f"{self.agent}:detect"

    def write(self, relative: str, source: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source), encoding="utf-8")

    def with_agent(self, extra_import: str = "", reply: str = "{'seen': reading}") -> "Folder":
        self.write(f"{self.helper}.py", "FACTOR = 2\n")
        lines = [
            "import json",
            "import wactorz",
            *extra_import.splitlines(),
            f"from {self.helper} import FACTOR",
            "",
            f'@wactorz.agent(name="probe-{self.agent[-8:]}", requires={{"packages": ["numpy"]}})',
            "def detect(reading: dict) -> dict:",
            f"    return {reply}",
        ]
        self.write(f"{self.agent}.py", "\n".join(lines) + "\n")
        return self


@pytest.fixture(name="folder")
def folder_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Folder]:
    folder = Folder(tmp_path / "imu_anomaly")
    monkeypatch.syspath_prepend(str(folder.root))
    yield folder
    for name in list(sys.modules):
        if name.startswith((folder.agent, folder.helper)):
            del sys.modules[name]


def imported(folder: Folder) -> Any:
    return importlib.import_module(folder.agent)


# ── Main: what is sent ────────────────────────────────────────────────────────


class TestPacking:
    def test_the_folder_goes_python_files_only(self, folder: Folder) -> None:
        folder.with_agent()
        folder.write("utils/filters.py", "X = 1\n")
        folder.write("imu_model.npz", "not code")
        folder.write("__pycache__/stale.py", "X = 1\n")
        folder.write(".hidden/secret.py", "X = 1\n")
        folder.write("venv/pyvenv.cfg", "home = /usr\n")
        folder.write("venv/lib/site.py", "X = 1\n")
        imported(folder)

        bundle = bundle_for(folder.target)

        assert set(bundle["files"]) == {
            f"{folder.agent}.py",
            f"{folder.helper}.py",
            "utils/filters.py",
        }
        assert bundle["sha256"] == digest(bundle["files"])

    def test_a_module_in_a_package_sends_the_package(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        name = f"pkg_{uuid.uuid4().hex[:8]}"
        (tmp_path / name).mkdir()
        (tmp_path / name / "__init__.py").write_text("")
        (tmp_path / name / "agent.py").write_text("from . import maths\n")
        (tmp_path / name / "maths.py").write_text("X = 1\n")
        (tmp_path / "beside.py").write_text("X = 1\n")
        monkeypatch.syspath_prepend(str(tmp_path))
        try:
            importlib.import_module(f"{name}.agent")

            bundle = bundle_for(f"{name}.agent:detect")
        finally:
            for loaded in [n for n in sys.modules if n.startswith(name)]:
                del sys.modules[loaded]

        assert set(bundle["files"]) == {
            f"{name}/__init__.py",
            f"{name}/agent.py",
            f"{name}/maths.py",
        }

    def test_the_standard_library_wactorz_and_installed_packages_are_fine(
        self, folder: Folder
    ) -> None:
        folder.with_agent(extra_import="import numpy\nimport asyncio")
        imported(folder)

        assert bundle_for(folder.target)["files"]

    def test_a_package_this_machine_does_not_have_is_fine(self, folder: Folder) -> None:
        # Imported only where it is installed: the node's pip, not this folder.
        folder.with_agent(
            extra_import="try:\n    import torch_not_here\nexcept ImportError:\n    pass"
        )
        imported(folder)

        assert bundle_for(folder.target)["files"]

    def test_own_code_from_another_folder_is_refused_naming_it(
        self, folder: Folder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lab = f"mylab_{uuid.uuid4().hex[:8]}"
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / f"{lab}.py").write_text("def lowpass(x):\n    return x\n")
        monkeypatch.syspath_prepend(str(elsewhere))
        folder.with_agent(extra_import=f"from {lab} import lowpass")
        imported(folder)

        with pytest.raises(BundleError, match=f"outside its folder: {lab}") as refused:
            bundle_for(folder.target)
        assert "live in its folder" in str(refused.value)
        del sys.modules[lab]

    def test_a_folder_too_large_is_refused(
        self, folder: Folder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        folder.with_agent()
        imported(folder)
        monkeypatch.setattr(bundles, "BUNDLE_MAX_BYTES", 10)

        with pytest.raises(BundleError, match="over the 10"):
            bundle_for(folder.target)

    def test_a_module_that_is_not_there_is_refused(self) -> None:
        with pytest.raises(BundleError, match="cannot be imported"):
            bundle_for("no_such_module_anywhere:detect")


# ── A node: what it accepts ───────────────────────────────────────────────────


def a_bundle(files: dict[str, str]) -> dict[str, Any]:
    return {"sha256": digest(files), "files": files}


class TestTakingOneIn:
    @pytest.mark.parametrize(
        "files",
        [{"../evil.py": "X = 1"}, {"/abs.py": "X = 1"}, {"run.sh": "rm -rf /"}, {"a\\b.py": ""}],
        ids=["up", "absolute", "not-python", "backslash"],
    )
    def test_a_path_it_may_not_hold_is_refused(self, files: dict[str, str]) -> None:
        with pytest.raises(BundleError, match="not a file it may hold"):
            checked(a_bundle(files))

    def test_one_that_does_not_match_its_hash_is_refused(self) -> None:
        bundle = a_bundle({"agent.py": "X = 1"})
        bundle["files"]["agent.py"] = "X = 2"

        with pytest.raises(BundleError, match="does not match"):
            checked(bundle)

    def test_it_is_written_once_under_its_hash(self, tmp_path: Path) -> None:
        bundle = a_bundle({"agent.py": "X = 1\n", "sub/m.py": "Y = 2\n"})

        directory = write(bundle, tmp_path)

        assert directory == tmp_path / bundle["sha256"]
        assert (directory / "sub" / "m.py").read_text() == "Y = 2\n"
        assert write(bundle, tmp_path) == directory

    def test_an_older_copy_no_agent_runs_gives_way(self, tmp_path: Path) -> None:
        name = f"mod_{uuid.uuid4().hex[:8]}"
        old = write(a_bundle({f"{name}.py": "V = 1\n"}), tmp_path)
        new = write(a_bundle({f"{name}.py": "V = 2\n"}), tmp_path)
        try:
            prepare_import(old, tmp_path, set())
            assert importlib.import_module(name).V == 1

            prepare_import(new, tmp_path, set())

            assert importlib.import_module(name).V == 2
            assert str(old.resolve()) not in sys.path
        finally:
            sys.modules.pop(name, None)
            for entry in (str(old.resolve()), str(new.resolve())):
                if entry in sys.path:
                    sys.path.remove(entry)

    def test_a_module_another_running_agent_owns_is_refused(self, tmp_path: Path) -> None:
        name = f"mod_{uuid.uuid4().hex[:8]}"
        theirs = write(a_bundle({f"{name}.py": "V = 1\n"}), tmp_path)
        mine = write(a_bundle({f"{name}.py": "V = 2\n"}), tmp_path)
        try:
            prepare_import(theirs, tmp_path, set())
            importlib.import_module(name)

            with pytest.raises(BundleError, match=f"already has a module named {name}"):
                prepare_import(mine, tmp_path, {theirs.resolve()})
        finally:
            sys.modules.pop(name, None)
            if str(theirs.resolve()) in sys.path:
                sys.path.remove(str(theirs.resolve()))

    def test_a_module_named_like_one_installed_is_refused(self, tmp_path: Path) -> None:
        directory = write(a_bundle({"json.py": "X = 1\n"}), tmp_path)

        with pytest.raises(BundleError, match="a package installed here"):
            prepare_import(directory, tmp_path, set())
        assert str(directory.resolve()) not in sys.path


# ── A node running one ────────────────────────────────────────────────────────


@pytest.fixture(name="runner")
async def runner_fixture(tmp_path: Path) -> AsyncIterator[RecordingRunner]:
    runner = RecordingRunner(tmp_path / "node-state")
    await runner.supervisor.start()
    try:
        yield runner
    finally:
        await runner.stop_all()
        if runner.supervisor._watch_task:
            runner.supervisor._watch_task.cancel()
        for entry in [p for p in sys.path if bundles.BUNDLES_DIRNAME in p]:
            sys.path.remove(entry)


def node_spawn(folder: Folder, **more: Any) -> dict[str, Any]:
    """What main sends: the agent's folder, read from disk without importing it here."""
    files = {
        path.relative_to(folder.root).as_posix(): path.read_text(encoding="utf-8")
        for path in folder.root.rglob("*.py")
    }
    return {
        "name": "probe",
        "type": "module",
        "target": folder.target,
        "bundle": a_bundle(files),
        **more,
    }


class TestTheNodeBuildingOne:
    @pytest.fixture(name="unimported")
    def unimported_fixture(self, tmp_path: Path) -> Iterator[Folder]:
        """A folder the node has not got: not on this process's path at all."""
        folder = Folder(tmp_path / "on-main" / "imu_anomaly")
        yield folder
        for name in list(sys.modules):
            if name.startswith((folder.agent, folder.helper)):
                del sys.modules[name]

    async def test_it_builds_the_agent_from_the_code_sent(
        self, runner: RecordingRunner, unimported: Folder
    ) -> None:
        unimported.with_agent()

        await runner.spawn_agent(node_spawn(unimported))

        hosted = runner.hosted("probe")
        assert hosted is not None
        assert await hosted.run_task({"x": 1}) == {"seen": {"x": 1}}
        module_file = Path(sys.modules[unimported.agent].__file__ or "")
        assert module_file.is_relative_to(runner._bundles_dir.resolve())

    async def test_changed_code_sent_again_is_the_code_that_runs(
        self, runner: RecordingRunner, unimported: Folder
    ) -> None:
        unimported.with_agent(reply="{'version': 1}")
        await runner.spawn_agent(node_spawn(unimported))
        unimported.with_agent(reply="{'version': 2}")

        await runner.spawn_agent(node_spawn(unimported, replace=True))

        hosted = runner.hosted("probe")
        assert hosted is not None
        assert await hosted.run_task({}) == {"version": 2}
        assert len(list(runner._bundles_dir.iterdir())) == 1, "the old bundle is cleared away"

    async def test_one_that_does_not_match_its_hash_is_refused(
        self, runner: RecordingRunner, unimported: Folder
    ) -> None:
        unimported.with_agent()
        spawn = node_spawn(unimported, _migration_token="t")
        spawn["bundle"]["sha256"] = "0" * 64

        await runner.spawn_agent(spawn)

        assert runner.hosted("probe") is None
        (ack,) = [p for t, p, _r in runner.published if t.endswith("/spawn_ack")]
        assert "does not match" in ack["refused"]

    async def test_code_that_raises_on_import_is_refused_with_why(
        self, runner: RecordingRunner, unimported: Folder
    ) -> None:
        unimported.with_agent(extra_import="raise RuntimeError('no camera')")

        await runner.spawn_agent(node_spawn(unimported, _migration_token="t"))

        (ack,) = [p for t, p, _r in runner.published if t.endswith("/spawn_ack")]
        assert "its code did not load: no camera" in ack["refused"]

    async def test_the_manifest_says_it_takes_code(self, runner: RecordingRunner) -> None:
        await runner._publish_manifest()

        manifest = [p for t, p, _r in runner.published if t.endswith("/manifest")][-1]
        assert manifest["code_bundles"] is True


# ── Main sending one ──────────────────────────────────────────────────────────


@pytest.fixture(name="registered")
def registered_fixture(folder: Folder) -> Iterator[str]:
    folder.with_agent()
    with plugins.registered([imported(folder).detect]) as (plugin,):
        yield plugin.target


class TestMainSendingOne:
    @staticmethod
    def _main(**manifest: Any) -> SpawningMain:
        main = SpawningMain()
        main.actor.nodes.node_manifests["rpi"] = manifest
        return main

    async def test_a_node_that_takes_code_is_sent_it_with_its_packages(
        self, registered: str, folder: Folder
    ) -> None:
        main = self._main(code_bundles=True, agents=[])

        await main.spawn({"name": "probe", "type": "module", "target": registered})

        _, sent, _ = main.published_to("/spawn")
        assert f"{folder.agent}.py" in sent["bundle"]["files"]
        assert sent["install"] == ["numpy"]

    async def test_a_node_that_has_it_installed_is_sent_no_code(self, registered: str) -> None:
        main = self._main(code_bundles=True, agents=[registered])

        await main.spawn({"name": "probe", "type": "module", "target": registered})

        _, sent, _ = main.published_to("/spawn")
        assert "bundle" not in sent

    async def test_a_node_from_before_bundles_is_refused_with_the_way_out(
        self, registered: str
    ) -> None:
        main = self._main(agents=[])

        await main.spawn({"name": "probe", "type": "module", "target": registered})

        assert not [row for row in main.published if row[0].endswith("/spawn")]
        (log,) = [p for t, p, _ in main.published if t.endswith("/logs")]
        assert "Redeploy the node" in log["message"]

    async def test_code_from_outside_the_folder_is_refused_before_it_is_sent(
        self, folder: Folder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lab = f"mylab_{uuid.uuid4().hex[:8]}"
        (tmp_path / "elsewhere").mkdir()
        (tmp_path / "elsewhere" / f"{lab}.py").write_text("X = 1\n")
        monkeypatch.syspath_prepend(str(tmp_path / "elsewhere"))
        folder.with_agent(extra_import=f"import {lab}")
        main = self._main(code_bundles=True, agents=[])
        with plugins.registered([imported(folder).detect]) as (plugin,):
            await main.spawn({"name": "probe", "type": "module", "target": plugin.target})

        assert not [row for row in main.published if row[0].endswith("/spawn")]
        (log,) = [p for t, p, _ in main.published if t.endswith("/logs")]
        assert lab in log["message"]
        del sys.modules[lab]


class TestMigratingOne:
    async def test_it_moves_with_its_code_to_a_node_that_takes_it(
        self, registered: str, folder: Folder
    ) -> None:
        name = plugins.for_target(registered).name  # pyright: ignore[reportOptionalMemberAccess]
        main = MigratingMain(nodes={"nuc": online()}, local=(name,))
        main.actor.nodes.node_manifests["nuc"] = {"code_bundles": True, "agents": []}

        result = await main.migrate(name, "nuc")

        assert result["success"] is True, result
        config, node, _save = main.spawned_remote[0]
        assert node == "nuc"
        assert f"{folder.agent}.py" in config["bundle"]["files"]

    async def test_one_that_imports_from_outside_its_folder_stays(
        self, folder: Folder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lab = f"mylab_{uuid.uuid4().hex[:8]}"
        (tmp_path / "elsewhere").mkdir()
        (tmp_path / "elsewhere" / f"{lab}.py").write_text("X = 1\n")
        monkeypatch.syspath_prepend(str(tmp_path / "elsewhere"))
        folder.with_agent(extra_import=f"import {lab}")
        with plugins.registered([imported(folder).detect]) as (plugin,):
            main = MigratingMain(nodes={"nuc": online()}, local=(plugin.name,))
            main.actor.nodes.node_manifests["nuc"] = {"code_bundles": True, "agents": []}

            result = await main.migrate(plugin.name, "nuc")

        assert result["success"] is False
        assert lab in result["message"]
        assert "It stays on 'local'" in result["message"]
        assert main.local(plugin.name).stopped is False
        del sys.modules[lab]


#: A bundle a node hands back that is not the one main placed.
TAMPERED = {"sha256": "0" * 64, "files": {"agent.py": "steal()\n"}}


class TestOneLeavingANode:
    """A node hands back a library agent's state; its code is always main's own.

    The config a node returns carries the bundle it was running, and a node is
    not trusted to say what main, or another node, executes: main builds the
    agent from its own plugin coming home, and sends a node it moves on to the
    bundle main makes from its own copy of the code.
    """

    @staticmethod
    def _main(name: str) -> MigratingMain:
        main = MigratingMain(nodes={"rpi": online((name,)), "nuc": online()})
        for node in ("rpi", "nuc"):
            main.actor.nodes.node_manifests[node] = {"code_bundles": True, "agents": []}
        return main

    async def _hand_back(self, main: MigratingMain, name: str, target: str, to: str) -> None:
        """Start the move to ``to`` and deliver rpi's answer, a tampered bundle in it."""
        result = await main.migrate(name, to)
        assert result["success"] is True, result
        ((_topic, request),) = main.published_to("/migrate")
        answer = {
            "agent": name,
            "return_token": request["return_token"],
            "config": {"name": name, "type": "module", "target": target, "bundle": TAMPERED},
            "state": {"seen": 3},
        }
        await main.actor.migration.receive_state_return(
            "nodes/rpi/state_return", json.dumps(answer).encode()
        )

    async def test_another_node_is_sent_the_code_main_holds(self, registered: str) -> None:
        name = plugins.for_target(registered).name  # pyright: ignore[reportOptionalMemberAccess]
        main = self._main(name)

        await self._hand_back(main, name, registered, "nuc")

        ((config, node, _save),) = main.spawned_remote
        assert node == "nuc"
        assert config["bundle"] == bundle_for(registered)
        assert config["_initial_state"] == {"seen": 3}

    async def test_coming_home_it_is_built_from_mains_plugin(self, registered: str) -> None:
        name = plugins.for_target(registered).name  # pyright: ignore[reportOptionalMemberAccess]
        main = self._main(name)

        await self._hand_back(main, name, registered, "local")

        (spawned,) = main.spawned_local
        assert spawned["type"] == "module"
        assert spawned["target"] == registered
        assert "bundle" not in spawned
        assert spawned["_initial_state"] == {"seen": 3}

    async def test_the_bundle_it_discarded_is_said(self, registered: str) -> None:
        name = plugins.for_target(registered).name  # pyright: ignore[reportOptionalMemberAccess]
        main = self._main(name)

        await self._hand_back(main, name, registered, "nuc")

        assert any("node's was discarded" in n["message"] for n in main.notifications)

    async def test_a_next_node_that_cannot_take_it_is_refused_before_anything_stops(
        self, registered: str
    ) -> None:
        name = plugins.for_target(registered).name  # pyright: ignore[reportOptionalMemberAccess]
        main = self._main(name)
        # A node from before bundles, without the agent installed.
        main.actor.nodes.node_manifests["nuc"] = {"agents": []}

        result = await main.migrate(name, "nuc")

        assert result["success"] is False
        assert "It stays on 'rpi'" in result["message"]
        assert not main.published_to("/migrate")

    async def test_a_next_node_that_stops_taking_it_meanwhile_has_it_restarted_where_it_was(
        self, registered: str
    ) -> None:
        name = plugins.for_target(registered).name  # pyright: ignore[reportOptionalMemberAccess]
        main = self._main(name)
        result = await main.migrate(name, "nuc")
        assert result["success"] is True, result
        # Redeployed from an older release while the source was handing it back.
        main.actor.nodes.node_manifests["nuc"] = {"agents": []}
        ((_topic, request),) = main.published_to("/migrate")
        answer = {
            "agent": name,
            "return_token": request["return_token"],
            "config": {
                "name": name,
                "type": "module",
                "target": registered,
                "bundle": TAMPERED,
            },
            "state": {"seen": 3},
        }

        await main.actor.migration.receive_state_return(
            "nodes/rpi/state_return", json.dumps(answer).encode()
        )

        ((config, node, _save),) = main.spawned_remote
        assert node == "rpi"
        assert config.get("bundle") != TAMPERED
        assert any("is running on 'rpi' again" in n["message"] for n in main.notifications)
