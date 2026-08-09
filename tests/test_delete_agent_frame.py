"""Both delete paths announce the removal under the name the dashboard matches.

The frame carried a state snapshot, so the numbers on the page stayed correct
and the bug looked like nothing — but a snapshot only adds and updates agents.
Removal happens solely on this frame type, so a name the client does not match
leaves the deleted agent's card on screen until a reload.

The name is asserted here, on the producing side, because the frontend test can
only pin what it expects — which is how the two came to disagree.
"""

import inspect
import re

from wactorz.web import api_actors, events, ws

# The literal the dashboard compares against (frontend/src/io/WSClient.ts).
# Changing it means changing the client in the same commit.
DASHBOARD_EXPECTS = "delete_agent"


def test_the_constant_matches_what_the_dashboard_listens_for() -> None:
    assert events.DELETE_AGENT_FRAME == DASHBOARD_EXPECTS


def test_the_rest_delete_path_uses_the_constant() -> None:
    source = inspect.getsource(api_actors)
    assert '"type": events.DELETE_AGENT_FRAME' in source
    assert "lifecycle.delete_agent" not in _frame_types(source)


def test_the_websocket_delete_path_uses_the_constant() -> None:
    source = inspect.getsource(ws)
    assert '"type": events.DELETE_AGENT_FRAME' in source
    assert "lifecycle.delete_agent" not in _frame_types(source)


def _frame_types(source: str) -> str:
    """Only the `"type": "..."` literals, so a module attribute call like
    `lifecycle.delete_agent(...)` is not mistaken for a frame name."""
    return "\n".join(re.findall(r'"type":\s*"[^"]+"', source))
