"""Play the building's Tuesday onto MQTT, faults and all.

    python -m air_quality.publish                 # 07:00 to 19:00, one step a second
    python -m air_quality.publish --speed 3       # three steps a second
    python -m air_quality.publish --no-faults     # an ordinary Tuesday

Each step is five simulated minutes, one reading per room on
``sensors/air/<room>``, stamped with its simulated time (``ts``). It publishes
to the broker Wactorz itself is configured for (``MQTT_HOST``, ``MQTT_PORT``
and the rest, from the environment or ``.env``).
"""

import argparse
import asyncio
import json
import sys

from wactorz.config import CONFIG
from wactorz.core.mqtt import mqtt_client

from .plant import ROOMS, SEED, TUESDAY, readings

if sys.platform == "win32":
    # aiomqtt needs the selector loop; Windows defaults to the proactor loop.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


async def play(*, start_hour: float, end_hour: float, speed: float, faults: bool) -> None:
    start, end = TUESDAY + start_hour * 3600, TUESDAY + end_hour * 3600
    async with mqtt_client(CONFIG.mqtt_host, CONFIG.mqtt_port) as client:
        for reading in readings(start, end, seed=SEED, faults=faults):
            await client.publish(f"sensors/air/{reading['device']}", json.dumps(reading))
            # Spread over the step rather than sent together: a model on trial is
            # run beside the served one only while it is not busy with the last
            # reading, so a burst would leave most readings out of the trial.
            await asyncio.sleep(1 / (speed * len(ROOMS)))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--from", dest="start", type=float, default=7.0, help="hour to start at")
    parser.add_argument("--to", dest="end", type=float, default=19.0, help="hour to stop at")
    parser.add_argument("--speed", type=float, default=1.0, help="steps a second")
    parser.add_argument("--no-faults", action="store_true", help="play an ordinary Tuesday")
    args = parser.parse_args(argv)
    asyncio.run(
        play(start_hour=args.start, end_hour=args.end, speed=args.speed, faults=not args.no_faults)
    )


if __name__ == "__main__":
    main()
