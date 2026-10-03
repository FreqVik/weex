# service.py
import asyncio
import json
import os
from datetime import datetime
from dotenv import load_dotenv
from redis import Redis
from rq import Queue

from tel.models import create_tables
from tel.tele import TelegramSignalMonitor
from tel.tasks import save_signal_to_db


load_dotenv()

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
REDIS_CHANNEL = os.getenv("REDIS_SIGNAL_CHANNEL", "crypto_signals")


class SignalService:
    def __init__(self):
        self.redis_conn = Redis.from_url(REDIS_URL)
        self.queue = Queue("signals_queue", connection=self.redis_conn)
        self.monitor = TelegramSignalMonitor()

    def serialize_signal(self, signal: dict) -> str:
        """Converts signal dictionary to a JSON string safe for Redis transit."""
        payload = signal.copy()
        # Convert datetime objects to ISO strings if present
        for key, val in payload.items():
            if isinstance(val, datetime):
                payload[key] = val.isoformat()
        return json.dumps(payload)

    def publish_signal(self, signal: dict) -> None:
        """Immediate Pub/Sub broadcast for zero-latency listeners."""
        payload = self.serialize_signal(signal)
        self.redis_conn.publish(REDIS_CHANNEL, payload)

    def enqueue_db_save(self, signal: dict) -> None:
        """Queues persistent storage in RQ."""
        self.queue.enqueue(save_signal_to_db, signal)

    async def on_new_signal(self, signal: dict, event: object) -> None:
        """
        Async callback passed into TelegramSignalMonitor.listen().
        Offloads synchronous Redis and RQ calls without freezing the event loop.
        """
        # 1. Publish immediately to Redis Pub/Sub
        await asyncio.to_thread(self.publish_signal, signal)

        # 2. Enqueue background persistence
        await asyncio.to_thread(self.enqueue_db_save, signal)

        print(f"[*] Dispatched {signal['action']} {signal['pair']} to Redis & RQ")

    async def run(self) -> None:
        """Boots the database, connects Telethon, and begins listening."""
        create_tables()
        await self.monitor.start()
        print("[+] SignalService running. Listening for channel updates...")
        await self.monitor.listen(callback=self.on_new_signal)


if __name__ == "__main__":
    service = SignalService()
    asyncio.run(service.run())
