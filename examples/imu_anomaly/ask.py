"""Start the detector and ask it two questions from the same program.

The shortest way to see `wactorz.ask`: the system runs on this script's loop
as a task, the detector is asked for a verdict on a jolt and on a resting
reading, and the system is stopped. A broker on localhost:1883 and a trained
model (`python train.py`) are all it needs; no model API key.

    python ask.py
"""

import asyncio
import sys

from agent import detect

import wactorz

if sys.platform == "win32":
    # Wactorz watches the broker's socket, which Windows' default proactor
    # loop cannot do; it refuses to start on one and names this fix.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


async def main() -> None:
    system = asyncio.create_task(
        wactorz.serve(agents=[detect], minimal=True, web=False, state_dir="./state")
    )
    await asyncio.sleep(3)  # the broker connection and the agent come up
    if system.done():
        system.result()  # a refused start is reported here, not swallowed
    try:
        jolt = await wactorz.ask("imu-anomaly", {"ax": 9.0, "ay": -7.5, "az": 1.0})
        resting = await wactorz.ask("imu-anomaly", {"ax": 0.1, "ay": 0.0, "az": 1.0})
        print("jolt    ->", jolt)
        print("resting ->", resting)
    finally:
        system.cancel()
        await asyncio.gather(system, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
