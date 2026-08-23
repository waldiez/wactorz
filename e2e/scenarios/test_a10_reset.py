"""`wactorz-reset` leaves a clean state directory, and the system restarts empty.

Both halves, because either alone is satisfiable by something broken. A reset
that empties the directory and leaves the system holding its agents in memory is
not a reset; nor is one that reports success and leaves the files that put every
agent back on the next start.

Its own backend and its own state directory - this scenario deletes what it is
pointed at, and the shared state directory is where every other scenario's
evidence lives.

Last by file order, which is why the numbers are zero-padded: `a10` sorts before
`a1` without the zero, and a reset that runs first wipes the run.
"""

import subprocess
import sys
from collections.abc import Callable

from harness import backend, browser, waiting

AGENT = "weather-agent"


def _reset(instance: backend.Backend) -> subprocess.CompletedProcess[str]:
    """Run the reset command the way a person does, against this state directory."""
    return subprocess.run(
        [sys.executable, "-m", "wactorz.reset", "--all", "--state-dir", str(instance.state_dir)],
        cwd=backend.REPO_ROOT,
        env={
            **backend.environment(
                state_dir=instance.state_dir,
                port=instance.port,
                api_port=instance.api_port,
                llm="fake",
            )
        },
        capture_output=True,
        text=True,
        check=False,
    )


def test_reset_empties_the_state_directory_and_the_system_comes_back_empty(
    own_app: Callable[..., backend.Backend],
    browse: Callable[[backend.Backend], browser.Dashboard],
) -> None:
    instance: backend.Backend = own_app()
    backend.wait_until_settled(instance)

    instance.rest.chat("spawn a weather agent")
    waiting.until(
        lambda: instance.rest.state_of(AGENT) == "running",
        what=f"{AGENT!r} to exist before it is reset away",
        timeout=90.0,
        interval=0.5,
    )
    # Proving the reset did something requires there to have been something: an
    # empty directory is trivially clean, and a scenario that skipped this would
    # pass against a reset that does nothing at all.
    assert (instance.state_dir / "wactorz.log").exists(), (
        "the state directory has nothing in it, so a clean one afterwards proves nothing"
    )

    instance.interrupt()
    result = _reset(instance)
    assert result.returncode == 0, (
        f"wactorz-reset exited {result.returncode}:\n{result.stdout}\n{result.stderr}"
    )

    # What "clean" means, precisely. The reset empties the stores; it does not
    # delete the directory or the database files, and asserting that it did would
    # be asserting a behaviour the command does not have and should not gain -
    # a tool that removes its own schema is a tool that fails differently next
    # time. What must not survive is anything an agent wrote about itself, since
    # that is what puts it back on the next start.
    survivors = sorted(
        str(path.relative_to(instance.state_dir))
        for path in instance.state_dir.rglob("*")
        if path.is_file() and path.suffix in {".pkl", ".json"}
    )
    assert not survivors, f"agent state survived the reset: {survivors}"

    assert (instance.state_dir / "wactorz.db").exists(), (
        "the reset removed the database rather than emptying it"
    )


def test_the_system_restarts_with_no_agents_of_its_own(
    own_app: Callable[..., backend.Backend],
    browse: Callable[[backend.Backend], browser.Dashboard],
) -> None:
    """Restarted after a reset, it holds only the agents it always starts.

    Checked in the browser as well as through the API, because "empty" is
    something a person sees: an overview still showing a card for a deleted agent
    is the same bug arriving by a different route.
    """
    instance: backend.Backend = own_app()
    backend.wait_until_settled(instance)

    instance.rest.chat("spawn a weather agent")
    waiting.until(
        lambda: instance.rest.state_of(AGENT) == "running",
        what=f"{AGENT!r} to exist before the reset",
        timeout=90.0,
        interval=0.5,
    )
    instance.interrupt()
    assert _reset(instance).returncode == 0, "wactorz-reset failed"

    restarted: backend.Backend = own_app(state_dir=instance.state_dir)
    backend.wait_until_settled(restarted)

    assert restarted.rest.agent(AGENT) is None, (
        f"{AGENT!r} came back after a reset - the spawn registry survived it"
    )

    page: browser.Dashboard = browse(restarted)
    page.show("overview", dwell="readable")
    page.wait_for_card("main")
    assert not page.has_card(AGENT), f"the overview still shows a card for {AGENT!r} after a reset"
