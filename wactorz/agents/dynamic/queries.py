"""Reading back what agents have recorded: time series, detections, HA state."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass

if TYPE_CHECKING:
    from .hosts import QueryHost

    # Typing-only base: it states what the host must provide and is gone
    # at runtime, so the real MRO is exactly what it was.
    _Host = QueryHost
else:
    _Host = object

logger = logging.getLogger(__name__)


class QueriesMixin(_Host):
    """Mixed into AgentAPI; reads the actor through `self._actor`."""

    async def publish_world_state(self, key: str, data: Any, retain: bool = True) -> None:
        """Publish a piece of world state to the shared retained state hub.
        Other agents can read this without making a request — it's always there.

        Topic: agents/{agent_name}/data/{key}

        Usage:
            await agent.publish_world_state('person_present', {'present': True, 'zone': 'kitchen'})
            await agent.publish_world_state('energy', {'kwh': 2.3, 'cost': 0.45})
        """
        from ...core.topic_bus import get_topic_bus

        bus = get_topic_bus()
        if bus:
            await bus.state_hub.publish_agent_data(self.name, key, data)
        else:
            topic = f"agents/{self.name}/data/{key}"
            await self.publish(topic, data)

    async def read_world_state(self, topic: str, timeout: float = 2.0) -> Any | None:
        """Read a retained world state topic — returns immediately if cached,
        otherwise waits up to timeout seconds for the retained message.

        Usage:
            presence = await agent.read_world_state('home/presence/kitchen')
            energy   = await agent.read_world_state('home/energy/current')
            ha_state = await agent.read_world_state('home/state/light/light.living_room')
        """
        return await self.mqtt_get(topic, timeout=timeout)

    def query_ts(
        self,
        hours: float = 24,
        topic: str | None = None,
        entity_id: str | None = None,
        field: str | None = None,
        limit: int = 100_000,
        as_dataframe: bool = False,
    ) -> Any:
        """Query historical sensor readings from the time-series store.

        Returns a list of dicts by default. Set as_dataframe=True to get
        a pandas DataFrame (requires pandas installed).

        SYNCHRONOUS — do NOT await.

        Usage:
            # Get last 24h of temperature data
            rows = agent.query_ts(hours=24, field='temp')

            # Get as pandas DataFrame for ML
            df = agent.query_ts(hours=168, entity_id='sensor.kitchen_temp', as_dataframe=True)

            # Train a model
            from sklearn.ensemble import IsolationForest
            model = IsolationForest().fit(df[['value']])
            agent.persist('anomaly_model', model)
        """
        from ...core.persistence import get_db

        db = get_db()
        if not db:
            logger.warning("[%s] query_ts: persistence not initialised", self.name)
            return [] if not as_dataframe else None

        rows = db.query_sensor(
            hours=hours,
            topic=topic,
            entity_id=entity_id,
            field=field,
            limit=limit,
        )

        if as_dataframe:
            try:
                import pandas as pd

                return pd.DataFrame(rows)
            except ImportError:
                logger.warning("[%s] pandas not installed — returning list of dicts", self.name)
                return rows
        return rows

    def query_detections(
        self,
        hours: float = 24,
        agent_name: str | None = None,
        class_name: str | None = None,
        min_confidence: float = 0.0,
        limit: int = 50_000,
        as_dataframe: bool = False,
    ) -> Any:
        """Query historical object detections (YOLO, camera agents).

        Usage:
            # All person detections in last 12 hours
            rows = agent.query_detections(hours=12, class_name='person')

            # As DataFrame for analysis
            df = agent.query_detections(hours=48, min_confidence=0.8, as_dataframe=True)
        """
        from ...core.persistence import get_db

        db = get_db()
        if not db:
            return [] if not as_dataframe else None

        rows = db.query_detections(
            hours=hours,
            agent=agent_name,
            class_name=class_name,
            min_confidence=min_confidence,
            limit=limit,
        )

        if as_dataframe:
            try:
                import pandas as pd

                return pd.DataFrame(rows)
            except ImportError:
                return rows
        return rows

    def query_ha_states(
        self,
        hours: float = 24,
        entity_id: str | None = None,
        domain: str | None = None,
        limit: int = 50_000,
        as_dataframe: bool = False,
    ) -> Any:
        """Query historical Home Assistant state changes.

        Usage:
            # All light state changes in last week
            df = agent.query_ha_states(hours=168, domain='light', as_dataframe=True)

            # Specific entity history
            rows = agent.query_ha_states(hours=24, entity_id='sensor.kitchen_temp')
        """
        from ...core.persistence import get_db

        db = get_db()
        if not db:
            return [] if not as_dataframe else None

        rows = db.query_ha_states(
            hours=hours,
            entity_id=entity_id,
            domain=domain,
            limit=limit,
        )

        if as_dataframe:
            try:
                import pandas as pd

                return pd.DataFrame(rows)
            except ImportError:
                return rows
        return rows

    def ts_stats(self) -> dict[str, Any]:
        """Return row counts for all time-series tables.
        Useful for checking how much data is available before training.

        Usage:
            stats = agent.ts_stats()
            # {'sensor_readings': 145230, 'detections': 8920, ...}
        """
        from ...core.persistence import get_db

        db = get_db()
        if not db:
            return {}
        return db.stats()
