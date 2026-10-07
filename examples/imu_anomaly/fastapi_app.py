"""The detector inside a FastAPI app: one process, one event loop, both servers.

The web app owns the loop and the signals. Wactorz runs on that loop as a
task started in the app's lifespan and cancelled when the app stops; the
actors are stopped and their state written before the process exits. One
route reads the running agent through `wactorz.system()`, the other asks it
through `wactorz.ask()`.

    pip install fastapi uvicorn
    python fastapi_app.py                                        # any platform
    uvicorn fastapi_app:app --port 8000 --loop asyncio:SelectorEventLoop   # Windows
    uvicorn fastapi_app:app --port 8000                          # elsewhere

On Windows uvicorn builds a proactor loop by default, which cannot watch the
broker's socket; Wactorz refuses to start on it and says so. The `__main__`
block below asks uvicorn for a selector loop, so the script runs anywhere.

The dashboard stays on 8888; `web=False` would leave it off.
"""

import asyncio
from contextlib import asynccontextmanager, suppress

from agent import detect
from fastapi import FastAPI, HTTPException

import wactorz

AGENT = "imu-anomaly"


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(wactorz.serve(agents=[detect], minimal=True, state_dir="./state"))
    # A start that is refused raises inside the task; look once, so the app
    # refuses to start rather than reporting it when it stops.
    await asyncio.sleep(0)
    if task.done():
        task.result()
    app.state.wactorz = task
    yield
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


app = FastAPI(lifespan=lifespan)


def _actor() -> wactorz.FunctionAgent:
    system = wactorz.system()
    actor = system.registry.find_by_name(AGENT) if system is not None else None
    if not isinstance(actor, wactorz.FunctionAgent):
        raise HTTPException(503, "the detector is not running")
    return actor


@app.get("/status")
async def status() -> dict:
    """What the dashboard card shows, as JSON."""
    actor = _actor()
    return {
        "state": actor.state.value,
        "messages_processed": actor.metrics.messages_processed,
        "anomalies_total": int(actor.recall("anomalies_total", 0)),
    }


@app.post("/detect")
async def detect_now(reading: dict) -> dict:
    """Score one reading on demand.

    The reading goes to the agent as a task, the way chat sends one, and the
    function's result is the answer.
    """
    try:
        result = await wactorz.ask(AGENT, reading, timeout=30)
    except LookupError as exc:
        raise HTTPException(503, str(exc)) from exc
    except asyncio.TimeoutError as exc:
        raise HTTPException(504, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
    # A reading that scores as normal returns nothing, which arrives as {"result": None}.
    if not isinstance(result, dict) or "score" not in result:
        return {"anomaly": False}
    return {"anomaly": True, **result}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, port=8000, loop="asyncio:SelectorEventLoop")
