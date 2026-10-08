import asyncio
import os
import re
from typing import Optional, Dict, Any, Union, Callable, Awaitable
from dotenv import load_dotenv
from telethon import TelegramClient, events

load_dotenv()


class TelegramSignalMonitor:
    """Monitors Telegram channels and parses structured trade signals."""

    NUMBER_PATTERN = r"(\d[\d,]*(?:\.\d+)?|\.\d+)"

    def __init__(
        self,
        api_id: Optional[Union[str, int]] = None,
        api_hash: Optional[str] = None,
        channel: Optional[str] = None,
        session_name: str = "user_session",
    ):
        self.api_id = int(api_id or os.getenv("API_ID", 0))
        self.api_hash = api_hash or os.getenv("API_HASH", "")
        self.channel = channel or os.getenv("CHANNEL_USERNAME", "")

        # Try converting channel to integer if it's a raw channel ID
        if isinstance(self.channel, str) and (
            self.channel.startswith("-100") or self.channel.isdigit()
        ):
            self.channel = int(self.channel)

        if not self.api_id or not self.api_hash or not self.channel:
            raise ValueError(
                "API_ID, API_HASH, and CHANNEL_USERNAME must be provided or set in environment variables."
            )

        self.client = TelegramClient(session_name, self.api_id, self.api_hash)

    @staticmethod
    def clean_number(num_str: Optional[str]) -> Optional[float]:
        """Safely strips commas and parses to float, returning None on failure."""
        if not num_str:
            return None
        cleaned = num_str.replace(",", "").strip()
        try:
            return float(cleaned)
        except (ValueError, TypeError):
            return None

    @classmethod
    def parse_signal(cls, text: str) -> Optional[Dict[str, Any]]:
        """Parses raw post text into a structured signal dictionary."""
        if not text:
            return None

        t_lower = text.lower()
        has_tp = "tp" in t_lower or "target" in t_lower
        has_sl = "sl" in t_lower or "stop" in t_lower

        # Gate: must contain at least a Take Profit or Stop Loss
        if not (has_tp or has_sl):
            return None

        # 1. Action: Long, Short, Buy, or Sell
        action_match = re.search(
            r"\b(long|short(?: sell)?|buy|sell)\b", text, re.IGNORECASE
        )
        raw_action = action_match.group(1).upper() if action_match else "UNKNOWN"
        action = (
            "LONG"
            if raw_action in ["LONG", "BUY"]
            else ("SHORT" if "SHORT" in raw_action or raw_action == "SELL" else raw_action)
        )

        if action == "UNKNOWN":
            return None

        # 2. Pair: Matches symbols like WUSDT, SANDUSDT, APTUSDT, BTC/USDT, #SOLUSDT
        pair_match = re.search(
            r"#?([A-Z0-9]{2,10}(?:/|-)?(?:USDT|BUSD|USDC|PERP))\b",
            text,
            re.IGNORECASE,
        )
        pair = (
            pair_match.group(1).upper().replace("/", "").replace("-", "")
            if pair_match
            else "UNKNOWN"
        )

        if pair == "UNKNOWN":
            return None

        # 3. Entry: Check for explicit numerical entry; defaults to CMP if missing or if MARKET ORDER
        entry_match = re.search(
            rf"(?:entry|enter|buy|sell)\s*[:\-]?\s*(cmp|{cls.NUMBER_PATTERN})",
            text,
            re.IGNORECASE,
        )
        if entry_match:
            raw_val = entry_match.group(1).strip().upper()
            if "CMP" in raw_val:
                entry: Union[str, float] = "CMP"
            else:
                parsed_entry = cls.clean_number(raw_val)
                entry = parsed_entry if parsed_entry is not None else "CMP"
        else:
            # Fallback to CMP if marked market order or if no explicit price line was provided
            entry = "CMP"

        # 4. Take Profit: Handles numbers, hyphens (TP-0.013), or "open"
        tp: Union[float, str, None] = None
        tp_match = re.search(
            rf"(?:tp|tp\s*1|target)\s*[:\-]*(?:[ ]*)({cls.NUMBER_PATTERN}|leave\s*it\s*open|open|trailing)",
            text,
            re.IGNORECASE,
        )
        if tp_match:
            raw_tp = tp_match.group(1).strip()
            numeric_tp = cls.clean_number(raw_tp)
            tp = numeric_tp if numeric_tp is not None else "OPEN"

        # 5. Stop Loss: Handles numbers and hyphens (SL-0.020)
        sl_match = re.search(
            rf"(?:sl|stop\s*loss|stop)\s*[:\-]*(?:[ ]*){cls.NUMBER_PATTERN}",
            text,
            re.IGNORECASE,
        )
        sl = cls.clean_number(sl_match.group(1)) if sl_match else None

        # Strictly require a numeric Stop Loss for position risk calculation
        if sl is None:
            return None

        return {
            "action": action,
            "pair": pair,
            "entry": entry,
            "tp": tp,
            "sl": sl,
            "raw_snippet": text[:120].strip(),
        }

    async def start(self) -> None:
        """Starts and authenticates the Telethon client session."""
        await self.client.start()

    async def scan_history(self, limit: int = 100) -> list[Dict[str, Any]]:
        """Scans past messages and returns a list of all parsed signals."""
        print(f"Scanning @{self.channel} for the last {limit} messages...\n")
        signals = []

        async for message in self.client.iter_messages(self.channel, limit=limit):
            if not message.text:
                continue

            try:
                data = self.parse_signal(message.text)
                if data:
                    data["message_id"] = message.id
                    data["date"] = message.date
                    signals.append(data)

                    print("=" * 45)
                    print(f"Message ID: {message.id} | Date: {message.date}")
                    print(f"Action: {data['action']} | Pair: {data['pair']}")
                    print(f"Entry:  {data['entry']} | TP: {data['tp']} | SL: {data['sl']}")
                    print("=" * 45)
            except Exception as e:
                print(f"Skipping malformed message {message.id}: {e}")

        print(f"\nScan complete. Found and parsed {len(signals)} matching signals.")
        return signals

    async def listen(
        self, callback: Optional[Callable[[Dict[str, Any], Any], Awaitable[None]]] = None
    ) -> None:
        """Runs a persistent listener for incoming real-time channel messages."""
        print(f"Listening for new signals on @{self.channel}...")

        @self.client.on(events.NewMessage(chats=self.channel))
        async def handler(event):
            if not event.message.text:
                return

            signal = self.parse_signal(event.message.text)
            if signal:
                signal["message_id"] = event.message.id
                signal["date"] = event.message.date

                if callback:
                    await callback(signal, event)
                else:
                    print(f"\n[!] Live Signal Detected on {signal['pair']}: {signal}")

        await self.client.run_until_disconnected()


"""
async def main():
    monitor = TelegramSignalMonitor()
    await monitor.start()

    # Mode 1: Scan recent message history
    await monitor.scan_history(limit=100)

    # Mode 2: Or listen live in real-time (uncomment to run)
    # await monitor.listen()


if __name__ == "__main__":
    asyncio.run(main())
"""