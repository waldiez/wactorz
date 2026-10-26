"""A fake IMU: normal readings with an abnormal one now and then.

Publishes to the broker Wactorz itself is configured for, with the same
credentials: ``MQTT_HOST``, ``MQTT_PORT``, ``MQTT_USERNAME`` and
``MQTT_PASSWORD``, from the environment or a ``.env`` file. Each reading says
which device it is from and when it was taken (``ts``, Unix seconds).
"""

import asyncio
import json
import sys
import time

import numpy as np

from wactorz.config import CONFIG
from wactorz.core.mqtt import mqtt_client

if sys.platform == "win32":
    # aiomqtt needs the selector loop; Windows defaults to the proactor loop,
    # which has no add_reader. Wactorz does this for its own processes.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

DEVICE = "left-wrist"


async def main() -> None:
    rng = np.random.default_rng()
    async with mqtt_client(CONFIG.mqtt_host, CONFIG.mqtt_port) as client:
        for i in range(200):
            if i % 25 == 24:
                ax, ay, az = 9.0, -7.5, 1.0  # a jolt
            else:
                ax, ay, az = rng.normal((0.0, 0.0, 1.0), (0.3, 0.3, 0.2))
            reading = {
                "device": DEVICE,
                "ts": time.time(),
                "ax": round(float(ax), 3),
                "ay": round(float(ay), 3),
                "az": round(float(az), 3),
            }
            await client.publish(f"sensors/imu/{DEVICE}", json.dumps(reading))
            await asyncio.sleep(0.2)


if __name__ == "__main__":
    asyncio.run(main())
