"""Publish a few notes for the summariser to hear."""

import asyncio
import json
import sys

import aiomqtt

if sys.platform == "win32":
    # aiomqtt needs the selector loop; Windows defaults to the proactor loop,
    # which has no add_reader. Wactorz does this for its own processes.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

NOTES = [
    "Met with Dana on 3 March about the pump on line 2. Flow drops to 2.1 l/min "
    "every evening around 19:00; she thinks the filter is clogging. Replace it "
    "Friday and watch the overnight numbers.",
    "Firmware 1.4.2 on the left-wrist IMU fixed the drift we saw last week. "
    "Right wrist still on 1.4.1, update it before the demo on the 10th.",
    "Grocery: oat milk, 2 kg flour, batteries for the door sensor.",
]


async def main() -> None:
    async with aiomqtt.Client("localhost", 1883) as client:
        for i, text in enumerate(NOTES):
            await client.publish(f"notes/raw/{i}", json.dumps({"text": text}))
            await asyncio.sleep(0.5)
        # Plain text works too: it arrives at the agent as {"raw": text}.
        await client.publish("notes/raw/plain", "Call the dentist back on Tuesday.")


if __name__ == "__main__":
    asyncio.run(main())
