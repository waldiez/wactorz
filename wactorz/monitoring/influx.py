"""
InfluxDB 2.x integration for Wactorz — chat log writer.

Activated when INFLUX_URL and INFLUX_TOKEN are set.
Writes one point per conversation turn to the measurement `wactorz_chat`.

Required env vars:
  INFLUX_URL     — e.g. http://localhost:8086
  INFLUX_TOKEN   — InfluxDB API token
  INFLUX_ORG     — organisation name (default: "wactorz")
  INFLUX_BUCKET  — bucket name       (default: "wactorz")
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_client = None
_write_api = None


def setup_influx() -> bool:
    """
    Configure the InfluxDB write client.
    Returns True if successfully set up, False if disabled or unavailable.
    Idempotent — safe to call multiple times.
    """
    global _client, _write_api

    url = os.getenv("INFLUX_URL", "").rstrip("/")
    token = os.getenv("INFLUX_TOKEN", "")
    if not url or not token:
        return False

    try:
        from influxdb_client import InfluxDBClient, WriteOptions  # type: ignore  # noqa: F401
        from influxdb_client.client.write_api import ASYNCHRONOUS  # type: ignore
    except ImportError:
        logger.warning("influxdb-client not installed — run: pip install 'wactorz[influx]'")
        return False

    org = os.getenv("INFLUX_ORG", "wactorz")
    bucket = os.getenv("INFLUX_BUCKET", "wactorz")

    _client = InfluxDBClient(url=url, token=token, org=org)
    _write_api = _client.write_api(write_options=ASYNCHRONOUS)

    logger.info(
        "InfluxDB enabled → %s  org=%s  bucket=%s",
        url,
        org,
        bucket,
    )
    # store bucket name for writes
    _write_api._wactorz_bucket = bucket
    _write_api._wactorz_org = org
    return True


def write_chat(agent_name: str, role: str, content: str, ts: float | None = None) -> None:
    """Write one chat turn as an InfluxDB line-protocol point (fire-and-forget)."""
    if _write_api is None:
        return
    try:
        import time as _t

        from influxdb_client import Point  # type: ignore

        point = (
            Point("wactorz_chat")
            .tag("agent", agent_name)
            .tag("role", role)
            .field("content", content)
            .field("length", len(content))
            .time(int((ts or _t.time()) * 1_000_000_000))  # nanoseconds
        )
        bucket = getattr(_write_api, "_wactorz_bucket", "wactorz")
        org = getattr(_write_api, "_wactorz_org", "wactorz")
        _write_api.write(bucket=bucket, org=org, record=point)
    except Exception as exc:
        logger.debug("InfluxDB write_chat failed: %s", exc)


def shutdown_influx() -> None:
    """Flush pending writes and close the client."""
    global _client, _write_api
    if _write_api is not None:
        try:
            _write_api.close()
        except Exception:
            pass
        _write_api = None
    if _client is not None:
        try:
            _client.close()
        except Exception:
            pass
        _client = None
