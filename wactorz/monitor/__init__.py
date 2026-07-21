"""Monitor package — the aiohttp monitor server, split into focused modules.

``runtime`` holds the shared mutable state; sibling modules (extracted from
``wactorz.monitor_server``) import it as ``from . import runtime`` and read
attributes at use time. ``wactorz.monitor_server`` remains the compatibility
façade for external importers.
"""
