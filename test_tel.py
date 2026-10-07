import asyncio
from telethon import TelegramClient
import os
from dotenv import load_dotenv
import re
from typing import Optional, Dict, Any, Union

load_dotenv()

API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
TARGET_CHANNEL = int(os.getenv("CHANNEL_USERNAME"))

client = TelegramClient("user_session", API_ID, API_HASH)

def clean_number(num_str: Optional[str]) -> Optional[float]:
    """Safely strips commas and parses to float, returning None on failure."""
    if not num_str:
        return None
    cleaned = num_str.replace(",", "").strip()
    try:
        return float(cleaned)
    except (ValueError, TypeError):
        return None

# Pattern requiring at least one numeric digit (handles commas & decimals)
NUMBER_PATTERN = r"(\d[\d,]*(?:\.\d+)?|\.\d+)"

def parse_signal_flexible(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None

    t_lower = text.lower()
    has_entry = "entry" in t_lower or "market order" in t_lower
    has_tp = "tp" in t_lower or "target" in t_lower
    has_sl = "sl" in t_lower or "stop" in t_lower

    # Gate: must indicate an entry/order style and at least a TP or SL
    if not (has_entry and (has_tp or has_sl)):
        return None

    # 1. Action: Long, Short, Buy, or Sell
    action_match = re.search(r"\b(long|short(?: sell)?|buy|sell)\b", text, re.IGNORECASE)
    raw_action = action_match.group(1).upper() if action_match else "UNKNOWN"
    action = "LONG" if raw_action in ["LONG", "BUY"] else ("SHORT" if "SHORT" in raw_action or raw_action == "SELL" else raw_action)

    # 2. Pair: Matches symbols like SANDUSDT, APTUSDT, BTC/USDT, #SOL
    pair_match = re.search(r"#?([A-Z0-9]{2,10}(?:/|-)?(?:USDT|BUSD|USDC|PERP))\b", text, re.IGNORECASE)
    pair = pair_match.group(1).upper().replace("/", "").replace("-", "") if pair_match else "UNKNOWN"

    if pair == "UNKNOWN":
        return None

    # 3. Entry: Check for explicit entry line; fallback to CMP if MARKET ORDER
    entry_match = re.search(rf"entry\s*[:\-]?\s*(cmp|{NUMBER_PATTERN})", text, re.IGNORECASE)
    if entry_match:
        raw_val = entry_match.group(1).strip().upper()
        if "CMP" in raw_val:
            entry: Union[str, float] = "CMP"
        else:
            parsed_entry = clean_number(raw_val)
            entry = parsed_entry if parsed_entry is not None else "CMP"
    elif "market order" in t_lower:
        entry = "CMP"
    else:
        entry = "N/A"

    # 4. Take Profit: Handles numbers OR phrases like "leave it open", "open"
    tp: Union[float, str, None] = None
    tp_match = re.search(rf"(?:tp|tp\s*1|target)\s*[:\-]?\s*({NUMBER_PATTERN}|leave\s*it\s*open|open|trailing)", text, re.IGNORECASE)
    if tp_match:
        raw_tp = tp_match.group(1).strip()
        numeric_tp = clean_number(raw_tp)
        if numeric_tp is not None:
            tp = numeric_tp
        else:
            tp = "OPEN"

    # 5. Stop Loss
    sl_match = re.search(rf"(?:sl|stop\s*loss)\s*[:\-]?\s*{NUMBER_PATTERN}", text, re.IGNORECASE)
    sl = clean_number(sl_match.group(1)) if sl_match else None

    # Skip if neither a valid TP nor an SL could be extracted
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
    await client.start()
    print(f"Scanning @{TARGET_CHANNEL} with flexible parser...\n")
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
    asyncio.run(main())