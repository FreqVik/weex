import os
import ccxt
from dotenv import load_dotenv

load_dotenv()

class Test:
    def __init__(self):
        self.exchange = ccxt.weex({
            'apiKey': os.getenv('WEEX_API_KEY'),
            'secret': os.getenv('WEEX_SECRET_KEY'),
            'password': os.getenv('WEEX_PASSPHRASE'),
            'enableRateLimit': True,
            'options': {
                'defaultType': 'swap',  # Sets futures/swap as primary market type
            }
        })

    def get_spot_balance(self):
        return self.exchange.fetch_balance(params={'type': 'spot'})

    def get_futures_balance(self):
        # Queries WEEX contract/derivatives wallet
        return self.exchange.fetch_balance(params={'type': 'swap'})


if __name__ == "__main__":
    test = Test()

    # 1. Fetch Spot
    try:
        spot = test.get_spot_balance()
        non_zero_spot = {k: v for k, v in spot.get('total', {}).items() if v > 0}
        print(f"Spot Balances: {non_zero_spot}")
    except Exception as e:
        print(f"Error fetching spot: {e}")

    # 2. Fetch Futures / Swap
    try:
        futures = test.get_futures_balance()
        non_zero_futures = {k: v for k, v in futures.get('total', {}).items() if v > 0}
        print(f"Futures Balances (Total): {non_zero_futures}")

        # Show detailed breakdown for USDT if present
        usdt_info = futures.get('USDT', {})
        if usdt_info:
            print(f"USDT Futures -> Total: {usdt_info.get('total')} | Free/Available: {usdt_info.get('free')} | Used/Margin: {usdt_info.get('used')}")
    except Exception as e:
        print(f"Error fetching futures balance: {e}")