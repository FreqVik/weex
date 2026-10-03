# executor/listener.py
import json
import os
import sys
from datetime import datetime, timezone
from dotenv import load_dotenv
from redis import Redis

from executor.service import TradeExecutionService

load_dotenv()

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
INPUT_CHANNEL = os.getenv("REDIS_SIGNAL_CHANNEL", "crypto_signals")
OUTPUT_CHANNEL = os.getenv("REDIS_TRADES_CHANNEL", "trades")


class TradeExecutorListener:
    def __init__(self):
        self.redis_conn = Redis.from_url(REDIS_URL)
        self.pubsub = self.redis_conn.pubsub()
        self.executor = TradeExecutionService(risk_usdt=20.0)

    def publish_trade(self, trade_data: dict) -> None:
        """Publishes verified successful trade execution payload to the 'trades' channel."""
        payload = trade_data.copy()
        payload["executed_at"] = datetime.now(timezone.utc).isoformat()

        serialized = json.dumps(payload, default=str)
        self.redis_conn.publish(OUTPUT_CHANNEL, serialized)
        print(f"[✓] Published verified trade to Redis channel '{OUTPUT_CHANNEL}'")

    def run(self) -> None:
        """Listens for signals on Redis Pub/Sub."""
        self.pubsub.subscribe(INPUT_CHANNEL)
        print(f"[+] Executor listening on Redis channel: '{INPUT_CHANNEL}'")
        print(f"[+] Output channel for successful executions: '{OUTPUT_CHANNEL}'")

        for message in self.pubsub.listen():
            if message["type"] != "message":
                continue

            raw_data = message["data"]
            if isinstance(raw_data, bytes):
                raw_text = raw_data.decode("utf-8")
            else:
                raw_text = str(raw_data)

            try:
                signal = json.loads(raw_text)
            except json.JSONDecodeError:
                print(f"[!] Skipped malformed JSON payload: {raw_text}")
                continue

            print("\n" + "=" * 55)
            print(f"[*] Received Signal: {signal.get('action')} {signal.get('pair')}")
            print(f"    Entry: {signal.get('entry')} | TP: {signal.get('tp')} | SL: {signal.get('sl')}")

            try:
                result = self.executor.execute_signal(signal)

                # Check execution outcome
                status = result.get("status")

                if status == "success":
                    self.publish_trade(result)
                    print(f"[+] Execution completed successfully for {result.get('symbol')} (Order ID: {result.get('order_id')})")
                elif status == "skipped":
                    print(f"[⚠] Signal skipped: {result.get('reason')}")
                    print("[!] Skipping publication to 'trades' channel.")
                else:
                    # status == 'failed'
                    print(f"[❌] Order rejected by exchange: {result.get('reason')}")
                    print("[!] Skipping publication to 'trades' channel.")

            except Exception as e:
                # Catches unexpected socket, runtime, or connection exceptions
                print(f"[❌] Unexpected Execution Exception: {e}")
                print("[!] Skipping publication to 'trades' channel.")

            print("=" * 55)


if __name__ == "__main__":
    listener = TradeExecutorListener()
    try:
        listener.run()
    except KeyboardInterrupt:
        print("\nStopping Trade Executor.")
        sys.exit(0)