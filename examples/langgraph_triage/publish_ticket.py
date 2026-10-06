"""Publish a few support tickets for the graph to triage."""

import asyncio
import json
import sys

import aiomqtt

if sys.platform == "win32":
    # aiomqtt needs the selector loop; Windows defaults to the proactor loop,
    # which has no add_reader. Wactorz does this for its own processes.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

TICKETS = [
    {"id": "T-101", "text": "The pump on line 2 stopped and the whole line is down. Urgent."},
    {"id": "T-102", "text": "We were charged twice on the March invoice, please refund one."},
    {"id": "T-103", "text": "Could you add support for exporting the weekly report as CSV?"},
    {"id": "T-104", "text": "How do I change the alert threshold for a sensor?"},
]


async def main() -> None:
    async with aiomqtt.Client("localhost", 1883) as client:
        for ticket in TICKETS:
            await client.publish("tickets/new", json.dumps(ticket))
            await asyncio.sleep(0.3)


if __name__ == "__main__":
    asyncio.run(main())
