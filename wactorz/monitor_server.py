"""Compatibility shim for the pre-split monitor module.

The monitor now lives in the :mod:`wactorz.web` package: ``app`` (route
table + entry point), ``runtime`` (shared mutable state), and one module per
concern — ``chat``, ``ws``, ``mqtt``, ``events``, ``cost``, ``lifecycle``,
``static_site``, ``api_actors``, ``api_system``, ``api_reset``.

This module exists so the ``wactorz-monitor`` console script, the
``python -m wactorz.monitor_server`` invocation, and any external importer keep
working. **New code should import from ``wactorz.web.*`` directly.**
"""

import os

from ._bootstrap import WACTORZ_BOOTSTRAP  # noqa: F401 # pylint: disable=unused-import
from .web import runtime
from .web.app import build_app, cli_main, main

__all__ = ["build_app", "cli_main", "main"]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Wactorz Monitor Server")
    parser.add_argument("--broker", default=os.getenv("WACTORZ_BROKER", "localhost"))
    parser.add_argument("--mqtt-port", type=int, default=1883)
    parser.add_argument("--mqtt-ws-port", type=int, default=int(os.getenv("MQTT_WS_PORT", "9001")))
    parser.add_argument("--ws-port", type=int, default=int(os.getenv("MONITOR_PORT", "8888")))
    args = parser.parse_args()

    runtime.MQTT_BROKER = args.broker
    runtime.MQTT_PORT = args.mqtt_port
    runtime.MQTT_WS_PORT = args.mqtt_ws_port
    runtime.WS_PORT = args.ws_port

    cli_main()
