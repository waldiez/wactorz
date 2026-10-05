import asyncio
from contextlib import asynccontextmanager, suppress
from fastapi import FastAPI
import wactorz

@wactorz.agent(
    name="imu-anomaly",
    subscribes="sensors/imu/#",
    publishes="anomalies/imu",
    description="Flags IMU readings the trained model calls abnormal.",
    requires={"ram_mb": 128, "packages": ["numpy"]},
)
def detect(reading: dict) -> dict | None:
    score = MODEL.score(reading)
    return {"score": score, "reading": reading} if score > 4.0 else None

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(wactorz.serve(agents=[detect], minimal=True))
    yield
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task  # the system is stopped, and its state written, before the host exits

app = FastAPI(lifespan=lifespan)