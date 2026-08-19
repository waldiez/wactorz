"""The fixture that keeps tests off a real broker.

Worth its own tests because its failure mode is invisible where it matters
least. On a machine with mosquitto running, a test that reaches the broker
connects and passes; the same test fails in CI, where nothing is listening. The
gap shows up as a green local run and a red pipeline, which is the slowest way
to find anything.

The subtle half is that patching `wactorz.core.mqtt.mqtt_client` is not enough.
A dozen modules do `from ..core.mqtt import mqtt_client`, binding the function
into their own namespace, and those bindings keep pointing at the real factory
however thoroughly the defining module is patched.
"""

import importlib
import sys

import pytest

from tests.conftest import real_mqtt_client
from wactorz.core import mqtt

#: A sample of the modules that bind `mqtt_client` into their own namespace.
#:
#: Imported so the derived check below has something to find even when the
#: worker running this file has not loaded them for other reasons. Not a
#: complete roster, and not meant to be.
_MODULES_THAT_IMPORT_IT = (
    "wactorz.agents.delegation",
    "wactorz.agents.manifests",
    "wactorz.agents.nodes",
    "wactorz.agents.migration",
    "wactorz.agents.llm_bridge",
    "wactorz.web.mqtt",
)


class TestTheGuardReachesEveryBinding:
    """Patching the defining module alone leaves the importers connected."""

    def test_the_defining_module_is_patched(self) -> None:
        with pytest.raises(ConnectionRefusedError):
            mqtt.mqtt_client("localhost", 1883)

    def test_no_module_is_left_holding_the_real_factory(self) -> None:
        """Derived rather than listed, because a list goes stale.

        Which modules import the name changes as code moves between them — one
        stopped importing it in this very refactor. Asking the question of
        whatever is loaded keeps the guard honest without anyone maintaining a
        roster of importers.
        """
        for name in _MODULES_THAT_IMPORT_IT:
            importlib.import_module(name)

        leaked = [
            name
            for name, module in list(sys.modules.items())
            if name.startswith("wactorz")
            and getattr(module, "mqtt_client", None) is real_mqtt_client
        ]

        assert not leaked

    @pytest.mark.parametrize("module_name", _MODULES_THAT_IMPORT_IT)
    def test_a_module_that_imported_the_name_is_patched(self, module_name: str) -> None:
        # These hold their own binding. Left alone, anything they open reaches
        # whatever is listening on the machine running the tests.
        module = importlib.import_module(module_name)

        with pytest.raises(ConnectionRefusedError):
            module.mqtt_client("localhost", 1883)

    def test_the_refusal_names_where_it_came_from(self) -> None:
        # A bare ConnectionRefusedError in a test looks like a real broker
        # being down, which is a different problem.
        with pytest.raises(ConnectionRefusedError, match="no broker in tests"):
            mqtt.mqtt_client("localhost", 1883)


class TestOptingOut:
    """A test that means to drive a connection can still say so."""

    @pytest.mark.real_mqtt_client
    def test_the_marker_leaves_the_factory_alone(self) -> None:
        # Not called: the point is that the real factory is in place, and
        # calling it here would try to connect.
        assert mqtt.mqtt_client is real_mqtt_client
