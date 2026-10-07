import asyncio
import os
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.utils import get_peer_id

load_dotenv()

# Replace with your actual credentials or ensure they exist in your .env
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
SESSION_NAME = "user_session"  # Name for the Telethon session file

# Paste your channel invite link or target title
INVITE_LINK = "https://t.me/+SuUExKbUfK4zMzg0"
FALLBACK_TITLE = "CWS Elite Community"     # <-- Used if link resolution throws


async def main():
    async with TelegramClient(SESSION_NAME, API_ID, API_HASH) as client:
        print("Logged in successfully. Searching for channel...")

        channel_entity = None

        # Method 1: Direct resolution via invite link
        try:
            channel_entity = await client.get_entity(INVITE_LINK)
            print("Successfully resolved entity via invite link.")
        except Exception as e:
            print(f"Direct link resolution failed ({e}). Falling back to dialog search...")

        # Method 2: Search existing dialogs by title if Method 1 fails
        if not channel_entity:
            async for dialog in client.iter_dialogs():
                if dialog.name and FALLBACK_TITLE.lower() in dialog.name.lower():
                    channel_entity = dialog.entity
                    print(f"Found match in joined dialogs: '{dialog.name}'")
                    break

        if not channel_entity:
            print(f"Error: Could not locate the channel using the link or title '{FALLBACK_TITLE}'.")
            return

        # Extract Telegram peer ID (typically formatted as -100xxxxxxxxxx)
        full_peer_id = get_peer_id(channel_entity)
        raw_id = getattr(channel_entity, "id", None)
        title = getattr(channel_entity, "title", "Unknown")

        print("\n" + "=" * 50)
        print(f"Channel Title : {title}")
        print(f"Raw ID        : {raw_id}")
        print(f"Full Peer ID  : {full_peer_id}  <-- Use this integer in your code")
        print("=" * 50)
        print(f"\nIn your test_tel.py script, set:\nTARGET_CHANNEL = {full_peer_id}\n")


if __name__ == "__main__":
    asyncio.run(main())