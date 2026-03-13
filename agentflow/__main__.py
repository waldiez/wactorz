"""
AgentFlow - Entry Point
"""

import asyncio

if __name__ == "__main__":
    from .cli import app
    asyncio.run(app())
