"""
ActorRegistry - Central registry and message router for all actors.
ActorSystem orchestrates startup, shutdown, and actor lifecycle.
"""

import asyncio
import logging
from typing import Optional, Type

from .actor import Actor, Message, MessageType

logger = logging.getLogger(__name__)


class ActorRegistry:
    """Maintains a map of all living actors and routes messages between them."""

    def __init__(self):
        self._actors: dict[str, Actor] = {}
        self._lock = asyncio.Lock()

    async def register(self, actor: Actor):
        async with self._lock:
            actor._registry = self
            self._actors[actor.actor_id] = actor
            logger.info(f"[Registry] Registered {actor.name} ({actor.actor_id[:8]})")

    async def unregister(self, actor_id: str):
        async with self._lock:
            if actor_id in self._actors:
                del self._actors[actor_id]
                logger.info(f"[Registry] Unregistered {actor_id[:8]}")

    async def deliver(self, target_id: str, msg: Message) -> bool:
        actor = self._actors.get(target_id)
        if actor is None:
            logger.warning(f"[Registry] Unknown target: {target_id[:8]}")
            return False
        await actor.receive(msg)
        return True

    async def broadcast(self, sender_id: str, msg_type: MessageType, payload=None):
        msg = Message(type=msg_type, sender_id=sender_id, payload=payload)
        for actor_id, actor in list(self._actors.items()):
            if actor_id != sender_id:
                await actor.receive(msg)

    def get(self, actor_id: str) -> Optional[Actor]:
        return self._actors.get(actor_id)

    def all_actors(self) -> list[Actor]:
        return list(self._actors.values())

    def find_by_name(self, name: str) -> Optional[Actor]:
        for actor in self._actors.values():
            if actor.name == name:
                return actor
        return None

    def __len__(self):
        return len(self._actors)


class ActorSystem:
    """Top-level orchestrator."""

    def __init__(self, mqtt_broker: str = "localhost", mqtt_port: int = 1883):
        self.registry     = ActorRegistry()
        self._mqtt_broker = mqtt_broker
        self._mqtt_port   = mqtt_port
        self._mqtt_client = None
        self._running     = False

    def _inject(self, actor: Actor):
        """Inject MQTT client + broker/port into an actor so it can publish and subscribe."""
        actor._mqtt_client = self._mqtt_client
        actor._mqtt_broker = self._mqtt_broker
        actor._mqtt_port   = self._mqtt_port

    async def start(self, *initial_actors: Actor):
        self._running = True
        self._mqtt_client = await _MQTTPublisher.create(self._mqtt_broker, self._mqtt_port)

        for actor in initial_actors:
            self._inject(actor)
            await self.registry.register(actor)
            await actor.start()

        logger.info(f"[ActorSystem] Started with {len(initial_actors)} actors.")

    async def spawn(self, actor_class: Type[Actor], **kwargs) -> Actor:
        """Spawn and register a new actor in the system."""
        actor = actor_class(**kwargs)
        self._inject(actor)
        await self.registry.register(actor)
        await actor.start()
        return actor

    async def stop_all(self):
        self._running = False
        actors = self.registry.all_actors()
        await asyncio.gather(*[a.stop() for a in actors], return_exceptions=True)
        if self._mqtt_client:
            await self._mqtt_client.disconnect()
        logger.info("[ActorSystem] All actors stopped.")

    async def run_forever(self):
        try:
            while self._running:
                await asyncio.sleep(1)
        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.info("[ActorSystem] Shutdown signal received.")
            await self.stop_all()


class _MQTTPublisher:
    """
    Persistent async MQTT publisher using a background task + queue.
    Automatically reconnects on failure. Never blocks callers.
    """

    def __init__(self):
        self._queue: asyncio.Queue = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
        self._available = False

    @classmethod
    async def create(cls, broker: str, port: int) -> "_MQTTPublisher":
        pub = cls()
        try:
            import aiomqtt  # noqa - check installed
            pub._task = asyncio.create_task(pub._run(broker, port))
            pub._available = True
            logger.info(f"[ActorSystem] MQTT publisher started -> {broker}:{port}")
        except ImportError:
            logger.warning("[ActorSystem] aiomqtt not installed. MQTT disabled.")
        except Exception as e:
            logger.warning(f"[ActorSystem] MQTT unavailable: {e}")
        return pub

    async def publish(self, topic: str, payload: str):
        if self._available:
            await self._queue.put((topic, payload))

    async def disconnect(self):
        if self._task:
            self._task.cancel()

    async def _run(self, broker: str, port: int):
        """Background loop: hold connection, drain publish queue."""
        import aiomqtt
        while True:
            try:
                async with aiomqtt.Client(broker, port) as client:
                    logger.info("[MQTT] Publisher connected.")
                    while True:
                        topic, payload = await self._queue.get()
                        await client.publish(topic, payload)
                        self._queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[MQTT] Publisher disconnected: {e}. Reconnecting in 3s...")
                await asyncio.sleep(3)


class _NoOpMQTT:
    async def publish(self, topic: str, payload: str):
        pass
    async def disconnect(self):
        pass