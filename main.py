"""Application entry point for the Telegram signal service."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

import uvicorn
from fastapi import FastAPI

from tel.routers import router
from tel.service import SignalMonitor


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    monitor = SignalMonitor()
    monitor_task = asyncio.create_task(monitor.run(), name="telegram-signal-monitor")
    application.state.monitor = monitor
    application.state.monitor_task = monitor_task
    try:
        yield
    finally:
        monitor_task.cancel()
        await asyncio.gather(monitor_task, return_exceptions=True)
        await monitor.client.disconnect()


app = FastAPI(title="Telegram Signal Service", lifespan=lifespan)
app.include_router(router)


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
    )
