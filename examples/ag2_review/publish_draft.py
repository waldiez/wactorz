"""Publish a draft for the reviewer to improve.

Run without arguments for the built-in draft, or pass a draft of your own:
``python publish_draft.py "Your own text."``
"""

import asyncio
import json
import sys

import aiomqtt

if sys.platform == "win32":
    # aiomqtt needs the selector loop; Windows defaults to the proactor loop,
    # which has no add_reader. Wactorz does this for its own processes.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

DRAFT = "We is pleased to announce the new pump line."


async def main(text: str) -> None:
    async with aiomqtt.Client("localhost", 1883) as client:
        await client.publish("drafts/new", json.dumps({"id": "d1", "text": text}))


if __name__ == "__main__":
    asyncio.run(main(" ".join(sys.argv[1:]) or DRAFT))
