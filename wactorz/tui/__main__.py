"""`python -m wactorz.tui`."""

from .app import run_async

if __name__ == "__main__":
    import asyncio

    asyncio.run(run_async())
