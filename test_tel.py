import asyncio
import os
import re
from typing import Optional, Dict, Any, Union
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.network import connection

load_dotenv()

API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
raw_channel = os.getenv("CHANNEL_USERNAME")

if not API_ID or not API_HASH:
    raise ValueError("Missing API_ID or API_HASH in .env")

try:
    TARGET_CHANNEL: Union[int, str] = int(raw_channel)
except (ValueError, TypeError):
    TARGET_CHANNEL = raw_channel

def clean_number(num_str: Optional[str]) -> Optional[float]:
    if not num_str:
        return None
    cleaned = num_str.replace(",", "").strip()
    try:
        return float(cleaned)
    except (ValueError, TypeError):
        return None

NUMBER_PATTERN = r"(\d[\d,]*(?:\.\d+)?|\.\d+)"

def parse_signal_flexible(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None

    t_lower = text.lower()
    has_entry = "entry" in t_lower or "market order" in t_lower
    has_tp = "tp" in t_lower or "target" in t_lower
    has_sl = "sl" in t_lower or "stop" in t_lower

    if not (has_entry and (has_tp or has_sl)):
        return None

    action_match = re.search(r"\b(long|short(?: sell)?|buy|sell)\b", text, re.IGNORECASE)
    raw_action = action_match.group(1).upper() if action_match else "UNKNOWN"
    action = "LONG" if raw_action in ["LONG", "BUY"] else ("SHORT" if "SHORT" in raw_action or raw_action == "SELL" else raw_action)

    pair_match = re.search(r"#?([A-Z0-9]{2,10}(?:/|-)?(?:USDT|BUSD|USDC|PERP))\b", text, re.IGNORECASE)
    pair = pair_match.group(1).upper().replace("/", "").replace("-", "") if pair_match else "UNKNOWN"

    if pair == "UNKNOWN":
        return None

    entry_match = re.search(rf"entry\s*[:\-]?\s*(cmp|{NUMBER_PATTERN})", text, re.IGNORECASE)
    if entry_match:
        raw_val = entry_match.group(1).strip().upper()
        if "CMP" in raw_val:
            entry: Union[str, float] = "CMP"
        else:
            parsed = clean_number(raw_val)
            entry = parsed if parsed is not None else "CMP"
    elif "market order" in t_lower:
        entry = "CMP"
    else:
        entry = "N/A"

    tp: Union[float, str, None] = None
    tp_match = re.search(rf"(?:tp|tp\s*1|target)\s*[:\-]?\s*({NUMBER_PATTERN}|leave\s*it\s*open|open|trailing)", text, re.IGNORECASE)
    if tp_match:
        raw_tp = tp_match.group(1).strip()
        numeric_tp = clean_number(raw_tp)
        tp = numeric_tp if numeric_tp is not None else "OPEN"

    sl_match = re.search(rf"(?:sl|stop\s*loss)\s*[:\-]?\s*{NUMBER_PATTERN}", text, re.IGNORECASE)
    sl = clean_number(sl_match.group(1)) if sl_match else None

    if tp is None and sl is None:
        return None

    return {
        "action": action,
        "pair": pair,
        "entry": entry,
        "tp": tp,
        "sl": sl,
        "raw_snippet": text[:120].strip()
    }

async def main():
    # Instantiate client strictly INSIDE the running loop
    client = TelegramClient(
        "user_session",
        int(API_ID),
        API_HASH,
        use_ipv6=False,
        connection=connection.ConnectionTcpAbridged
    )

    async with client:
        print(f"Authenticated successfully. Scanning {TARGET_CHANNEL}...")
        matched = 0

        async for message in client.iter_messages(TARGET_CHANNEL, limit=300):
            if not message.text:
                continue

            try:
                data = parse_signal_flexible(message.text)
                if data:
                    matched += 1
                    print("=" * 45)
                    print(f"Message ID: {message.id} | Date: {message.date}")
                    print(f"Action: {data['action']} | Pair: {data['pair']}")
                    print(f"Entry:  {data['entry']} | TP: {data['tp']} | SL: {data['sl']}")
                    print("=" * 45)
            except Exception as e:
                print(f"Skipping malformed message {message.id}: {e}")

        print(f"\nDone. Found and parsed {matched} matching signals.")

if __name__ == "__main__":
    # Clean previous broken sessions
    asyncio.run(main())