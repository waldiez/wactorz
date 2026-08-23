"""A node comes online and is listed - and the runner that did it needed nothing
from the `wactorz` package.

The second claim is about packaging, not liveness. `remote_runner.py` is copied
to a single-board machine on its own, so anything it imports from the package is
an import that cannot resolve there. The harness runs every node from a copy
outside the repository with `wactorz` made unimportable, so a runner that reaches
for it dies saying so instead of appearing to be a slow node.
"""

from collections.abc import Callable

from harness import backend, node


def test_a_node_comes_online_and_is_listed(
    app: backend.Backend, edge_node: Callable[..., node.Node]
) -> None:
    started = edge_node()
    node.wait_until_listed(started, app.rest)
    assert started.name in app.rest.node_names(), (
        f"the node is not in the list the dashboard is given: {app.rest.node_names()}"
    )


def test_the_runner_imports_nothing_from_the_wactorz_package(
    app: backend.Backend, edge_node: Callable[..., node.Node]
) -> None:
    """The runner ran to completion as a lone file, which is the property.

    Asserted by having run rather than by reading the source: a dynamic import
    is invisible to anything that greps, and it is exactly the kind that gets
    added without anyone noticing it broke deployment.
    """
    started = edge_node()
    node.wait_until_listed(started, app.rest)

    console = started.console()
    assert "single file" not in console, (
        f"the runner imported from the wactorz package, which is not present on an "
        f"edge node:\n{console[-2000:]}"
    )
    assert started.alive, f"the node runner exited:\n{console[-2000:]}"
