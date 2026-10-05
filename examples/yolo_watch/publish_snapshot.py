"""Publish an image file as a snapshot, for the detector to look at.

python publish_snapshot.py photo.jpg [door]
"""

import asyncio
import sys
from pathlib import Path

import aiomqtt

if sys.platform == "win32":
    # aiomqtt needs the selector loop; Windows defaults to the proactor loop,
    # which has no add_reader. Wactorz does this for its own processes.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


async def main(path: Path, camera: str) -> None:
    async with aiomqtt.Client("localhost", 1883) as client:
        # Bytes as they are: the agent receives them as {"raw": bytes}.
        await client.publish(f"camera/{camera}/snapshot", path.read_bytes())


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    image = Path(sys.argv[1])
    if not image.is_file():
        sys.exit(f"{image}: no such file")
    asyncio.run(main(image, sys.argv[2] if len(sys.argv) > 2 else "door"))
