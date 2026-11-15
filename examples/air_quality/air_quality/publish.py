# SPDX-License-Identifier: Apache-2.0
"""Play the building's Tuesday onto MQTT, faults and all.

    python -m air_quality.publish                 # 07:00 to 19:00, one step a second
    python -m air_quality.publish --speed 3       # three steps a second
    python -m air_quality.publish --no-faults     # an ordinary Tuesday
    python -m air_quality.publish --week --speed 60   # the healthy week first, in half a minute

Each step is five simulated minutes, one reading per room on
``sensors/air/<room>``, stamped with its simulated time (``ts``). It publishes
to the broker Wactorz itself is configured for (``MQTT_HOST``, ``MQTT_PORT``
and the rest, from the environment or ``.env``). ``--week`` plays the healthy
week the model was fitted on, so the recorder holds what the drift agent
compares the Tuesday against.
"""

import argparse
import asyncio
import json
import sys

from wactorz.config import CONFIG
from wactorz.core.mqtt import mqtt_client

from .plant import DAY_S, ROOMS, SEED, TUESDAY, WEEK, readings

if sys.platform == "win32":
    # aiomqtt needs the selector loop; Windows defaults to the proactor loop.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


async def play(*, start: float, end: float, speed: float, faults: bool) -> None:
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
    parser.add_argument(
        "--week", action="store_true", help="play the healthy week the model learned from instead"
    )
    args = parser.parse_args(argv)
    if args.week:
        start, end = WEEK, WEEK + 7 * DAY_S
    else:
        start, end = TUESDAY + args.start * 3600, TUESDAY + args.end * 3600
    asyncio.run(play(start=start, end=end, speed=args.speed, faults=not args.no_faults))


if __name__ == "__main__":
    main()
