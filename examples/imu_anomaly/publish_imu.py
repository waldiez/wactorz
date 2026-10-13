"""A fake IMU: normal readings with an abnormal one now and then."""

import asyncio
import json
import sys

import aiomqtt
import numpy as np

if sys.platform == "win32":
    # aiomqtt needs the selector loop; Windows defaults to the proactor loop,
    # which has no add_reader. Wactorz does this for its own processes.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


async def main() -> None:
    rng = np.random.default_rng()
    async with aiomqtt.Client("localhost", 1883) as client:
        for i in range(200):
            if i % 25 == 24:
                reading = {"ax": 9.0, "ay": -7.5, "az": 1.0}  # a jolt
            else:
                ax, ay, az = rng.normal((0.0, 0.0, 1.0), (0.3, 0.3, 0.2))
                reading = {
                    "ax": round(float(ax), 3),
                    "ay": round(float(ay), 3),
                    "az": round(float(az), 3),
                }
            await client.publish("sensors/imu/left-wrist", json.dumps(reading))
            await asyncio.sleep(0.2)


if __name__ == "__main__":
    asyncio.run(main())
