"""Reading back what agents have recorded: time series, detections, HA state.

The history lives in main's database. On main a query answers at once, and its
result may be used directly or awaited; on a node it is read from main over the
broker (see :mod:`wactorz.node.history`) and must be awaited. So
``await agent.query_ts(...)`` is the form that runs anywhere, and the bare call
is main's alone -- on a node it says so the moment its result is used.
"""

import logging
from collections.abc import Awaitable, Callable
from functools import partial
from typing import TYPE_CHECKING, Any

from ...core.persistence import get_db
from .awaitable import AwaitableDict, AwaitableList, awaitable_frame

if TYPE_CHECKING:
    from .agent import DynamicAgent
    from .hosts import QueryHost

    # Typing-only base: it states what the host must provide and is gone
    # at runtime, so the real MRO is exactly what it was.
    _Host = QueryHost
else:
    _Host = object

logger = logging.getLogger(__name__)

#: The database method answering each history query, named as the bridge to
#: main names them (see :mod:`wactorz.agents.main.history`).
_DB_METHODS = {
    "ts": "query_sensor",
    "detections": "query_detections",
    "ha_states": "query_ha_states",
}


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

        Await it: on a node the rows come from main over the broker. On main
        the bare call works too.

        Usage:
            # Get last 24h of temperature data
            rows = await agent.query_ts(hours=24, field='temp')

            # Get as pandas DataFrame for ML
            df = await agent.query_ts(hours=168, entity_id='sensor.kitchen_temp', as_dataframe=True)

            # Train a model, and keep it as a file under the state directory.
            # persist() keeps only JSON values on a node and in a migration, so
            # a model persisted there is dropped; a file stays where it is.
            import joblib
            from sklearn.ensemble import IsolationForest
            model = IsolationForest().fit(df[['value']])
            joblib.dump(model, f"{WACTORZ_STATE_DIR}/{agent.name}-anomaly_model.joblib")
        """
        filters = {"topic": topic, "entity_id": entity_id, "field": field}
        return self._history("query_ts", "ts", filters, hours, limit, as_dataframe)

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

        Await it: on a node the rows come from main over the broker.

        Usage:
            # All person detections in last 12 hours
            rows = await agent.query_detections(hours=12, class_name='person')

            # As DataFrame for analysis
            df = await agent.query_detections(hours=48, min_confidence=0.8, as_dataframe=True)
        """
        filters = {"agent": agent_name, "class_name": class_name, "min_confidence": min_confidence}
        return self._history("query_detections", "detections", filters, hours, limit, as_dataframe)

    def query_ha_states(
        self,
        hours: float = 24,
        entity_id: str | None = None,
        domain: str | None = None,
        limit: int = 50_000,
        as_dataframe: bool = False,
    ) -> Any:
        """Query historical Home Assistant state changes.

        Await it: on a node the rows come from main over the broker.

        Usage:
            # All light state changes in last week
            df = await agent.query_ha_states(hours=168, domain='light', as_dataframe=True)

            # Specific entity history
            rows = await agent.query_ha_states(hours=24, entity_id='sensor.kitchen_temp')
        """
        filters = {"entity_id": entity_id, "domain": domain}
        return self._history("query_ha_states", "ha_states", filters, hours, limit, as_dataframe)

    def ts_stats(self) -> Any:
        """Return row counts for all time-series tables.
        Useful for checking how much data is available before training.

        Await it: on a node the counts come from main over the broker.

        Usage:
            stats = await agent.ts_stats()
            # {'sensor_readings': 145230, 'detections': 8920, ...}
        """
        if self._actor.reads_history_from_main:
            return NotFetched("ts_stats", partial(_fetched_stats, self._actor))
        db = get_db()
        return AwaitableDict() if not db else AwaitableDict(db.stats())

    def _history(
        self,
        call: str,
        query: str,
        filters: dict[str, Any],
        hours: float,
        limit: int,
        as_dataframe: bool,
    ) -> Any:
        """One history query, answered here or -- on a node -- awaited from main.

        Filters left as None are dropped; the database methods treat a missing
        filter and a None one alike.
        """
        filters = {name: value for name, value in filters.items() if value is not None}
        if self._actor.reads_history_from_main:
            fetch = partial(
                _fetched_rows, self._actor, query, filters, hours, limit, as_dataframe, self.name
            )
            return NotFetched(call, fetch)
        db = get_db()
        if not db:
            logger.warning("[%s] %s: persistence not initialised", self.name, call)
            return None if as_dataframe else AwaitableList()
        rows = getattr(db, _DB_METHODS[query])(hours=hours, limit=limit, **filters)
        return _shaped(rows, as_dataframe, self.name)


class NotAwaited(TypeError):
    """A history read on a node used without being awaited."""

    @classmethod
    def on_a_node(cls, call: str) -> "NotAwaited":
        return cls(
            f"agent.{call}() reads main's history over the broker on a node, so it must "
            f"be awaited: write `await agent.{call}(...)`"
        )


class NotFetched:
    """A history read on a node, not yet awaited.

    Awaiting it reads from main. Using it any other way -- iterating it, taking
    its length, indexing it, calling a frame's methods on it -- fails at once
    with the fix, rather than handing back an empty result that looks like no
    history at all.
    """

    def __init__(self, call: str, fetch: Callable[[], Awaitable[Any]]) -> None:
        self._call = call
        self._fetch = fetch

    def __await__(self) -> Any:
        return self._fetch().__await__()

    def _refuse(self, *_args: Any, **_kwargs: Any) -> Any:
        raise NotAwaited.on_a_node(self._call)

    __iter__ = __len__ = __getitem__ = __contains__ = __bool__ = _refuse

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return self._refuse()

    def __repr__(self) -> str:
        return f"<agent.{self._call}() not yet awaited>"


async def _fetched_rows(
    actor: "DynamicAgent",
    query: str,
    filters: dict[str, Any],
    hours: float,
    limit: int,
    as_dataframe: bool,
    name: str,
) -> Any:
    """Rows read from main, shaped as main shapes its own; empty when main did not give them."""
    rows = await actor.read_history(query, filters, hours=hours, limit=limit)
    if rows is None:
        return None if as_dataframe else AwaitableList()
    return _shaped(rows, as_dataframe, name)


async def _fetched_stats(actor: "DynamicAgent") -> AwaitableDict:
    return AwaitableDict(await actor.read_history_stats() or {})


def _shaped(rows: list[dict[str, Any]], as_dataframe: bool, name: str) -> Any:
    """``rows`` as the caller asked: a list, or a frame when pandas is there to make one."""
    if as_dataframe:
        try:
            return awaitable_frame(rows)
        except ImportError:
            logger.warning("[%s] pandas not installed — returning list of dicts", name)
    return AwaitableList(rows)
