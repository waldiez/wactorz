"""Unpickling our own state files is safe only while nothing else can write them.

The server reads `<state>/<agent>/state.pkl` back with `pickle.load`, and unpickling
a file someone else placed is code execution. That is fine today, and it is fine for
three reasons rather than one:

1. **The wire is JSON.** A migrating agent's state is filtered to JSON-serialisable
   values by the runner and parsed with `json.loads` here, so nothing pickled ever
   crosses the broker.
2. **Path containment is enforced.** `agent_state_dir` refuses a name that climbs out,
   and every state path is built through it — pinned by `test_agent_name_paths.py`.
3. **Uploads cannot collide with it.** They live in a subdirectory of their own under
   names the server generates.

Layers 1 and 2 are code, and code that is already tested. Layer 3 is a *convention*,
and it is the one a future feature can break without noticing: a backup import, a
state-restore endpoint, an SFTP pull — anything that writes a caller-influenced path
under the state directory. So does adding a *new* unpickle site somewhere less
guarded.

These tests exist to make either of those fail in CI rather than in the field.
"""

import ast
from pathlib import Path

from wactorz.web import uploads

PACKAGE = Path(__file__).resolve().parents[1] / "wactorz"

#: Where the server is allowed to unpickle, and how many times in each.
#:
#: Counted per file rather than pinned per line, so moving code within a file does
#: not fail this, while a *new* call anywhere — including a sixth one in a file
#: already listed — does.
ALLOWED_UNPICKLE_SITES = {
    "core/actor.py": 2,  # legacy state, and the legacy file on the new path
    "core/persistence/legacy_pickle.py": 1,  # one-time import into SQLite
    "core/persistence/pickle_store.py": 1,  # the store itself
    "core/persistence/migrations.py": 1,  # baselines upgrade
}


def _unpickle_sites() -> dict[str, int]:
    """Every stdlib unpickle call under `wactorz/`, counted per file.

    Parsed rather than grepped, because `PersistenceAPI` calls
    `self.pickle.load(...)` — that is `PickleStore.load`, nothing to do with the
    stdlib, and a text search reports four of them. A tripwire that cries wolf is
    one that gets deleted.

    Does not see inside `catalogue_agents`' `AGENT_CODE`, which is a string
    literal here and runs on a node rather than against this state tree.
    """
    found: dict[str, int] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"load", "loads"}
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "pickle"
        ]
        if calls:
            found[path.relative_to(PACKAGE).as_posix()] = len(calls)
    return found


class TestNoNewUnpickleSite:
    """The 'careless feature' detector: a new place that unpickles."""

    def test_the_known_sites_are_the_only_ones(self) -> None:
        assert _unpickle_sites() == ALLOWED_UNPICKLE_SITES, (
            "unpickling is code execution if the file is not ours. A new site here "
            "needs the same argument the existing ones have: the path is built "
            "through agent_state_dir, and nothing untrusted can write to it."
        )

    def test_nothing_imports_the_unpickler_by_name(self) -> None:
        # `from pickle import loads` would slip straight past the check above.
        importers = [
            path.relative_to(PACKAGE).as_posix()
            for path in sorted(PACKAGE.rglob("*.py"))
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.ImportFrom) and node.module == "pickle"
        ]

        assert importers == []


class TestAnUploadCannotBecomeAStateFile:
    """`<state>/uploads/` is a directory the startup walk looks into.

    `legacy_pickle` and `migrations` both walk every directory under the state
    directory looking for `state.pkl`, and `uploads` is one of them. What stops an
    upload landing there is that its name is generated here, never taken from the
    client.
    """

    def test_a_stored_name_is_always_a_generated_id(self) -> None:
        assert uploads.is_id(uploads.new_id())

    def test_the_name_of_a_state_file_is_not_a_valid_id(self) -> None:
        for name in ("state.pkl", "../state.pkl", ".state.pkl.part"):
            assert not uploads.is_id(name)

    def test_a_client_filename_never_reaches_the_filesystem(self) -> None:
        # `safe_name` is for display; the stored name is `new_id()`. If these ever
        # met, an upload called `state.pkl` would be read by the startup walk.
        assert uploads.safe_name("state.pkl") == "state.pkl"
        assert not uploads.is_id(uploads.safe_name("state.pkl"))

    def test_uploads_live_below_the_state_directory_not_beside_agents(self, tmp_path: Path) -> None:
        directory = uploads.upload_dir(str(tmp_path))

        assert directory.parent == tmp_path
        assert directory.name == uploads.UPLOADS_DIRNAME
