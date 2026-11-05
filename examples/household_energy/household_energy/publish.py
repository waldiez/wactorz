"""Play a day of the house's meter onto MQTT.

    python -m household_energy.publish              # the Saturday, 06:00 to 23:00, ten minutes a second
    python -m household_energy.publish --speed 30   # faster
    python -m household_energy.publish --day 1      # the Saturday after, its runs drawn from the seed

Each reading is one simulated minute, on ``sensors/energy/meter``, stamped with
its simulated time (``ts``). It publishes to the broker Wactorz itself is
configured for (``MQTT_HOST``, ``MQTT_PORT`` and the rest, from the environment
or ``.env``).
"""

import argparse
import asyncio
import json
import sys

from wactorz.config import CONFIG
from wactorz.core.mqtt import mqtt_client

from .plant import DAY_S, SATURDAY, SEED, readings

#: Where the meter's readings go.
TOPIC = "sensors/energy/meter"

if sys.platform == "win32":
    # aiomqtt needs the selector loop; Windows defaults to the proactor loop.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


async def play(*, day: int, start_hour: float, end_hour: float, speed: float) -> None:
    midnight = SATURDAY + 7 * DAY_S * day
    start, end = midnight + start_hour * 3600, midnight + end_hour * 3600
    async with mqtt_client(CONFIG.mqtt_host, CONFIG.mqtt_port) as client:
        for reading in readings(start, end, SEED):
            await client.publish(TOPIC, json.dumps(reading))
            await asyncio.sleep(1 / speed)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument(
        "--day", type=int, default=0, help="which Saturday: 0 is the one written down"
    )
    parser.add_argument("--from", dest="start", type=float, default=6.0, help="hour to start at")
    parser.add_argument("--to", dest="end", type=float, default=23.0, help="hour to stop at")
    parser.add_argument("--speed", type=float, default=10.0, help="readings (minutes) a second")
    args = parser.parse_args(argv)
    asyncio.run(play(day=args.day, start_hour=args.start, end_hour=args.end, speed=args.speed))


if __name__ == "__main__":
    main()
