"""Web layer — the aiohttp server (dashboard SPA + REST + WebSocket + docs).

Split into focused modules: ``app`` (route table + entry point), ``runtime``
(shared mutable state), and one module per concern (``chat``, ``ws``, ``mqtt``,
``events``, ``cost``, ``lifecycle``, ``static_site``, ``api_actors``,
``api_system``, ``api_reset``).

``runtime`` holds the shared mutable state; sibling modules import it as
``from . import runtime`` and read attributes at use time. Named ``web`` to
avoid collision with ``wactorz.monitoring`` (Prometheus/OTel) and the
``MonitorActor`` agent. ``wactorz.monitor_server`` remains a back-compat shim.
"""
