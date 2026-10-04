# executor/service.py
import os
from typing import Optional, Dict, Any, List
from dotenv import load_dotenv
import ccxt

load_dotenv()


class TradeExecutionService:
    """
    Executes trades on WEEX based on parsed Telegram signals:
    - Entry == 'CMP' -> Market Order
    - Entry == float -> Limit Order
    - TP == 'OPEN'   -> No TP bracket
    - TP == float    -> Set Take-Profit
    - SL == float    -> Set Stop-Loss
    """

    def __init__(self, risk_usdt: float = 20.0, sandbox: bool = True):
        self.api_key = os.getenv("WEEX_API_KEY")
        self.secret = os.getenv("WEEX_SECRET_KEY")
        self.passphrase = os.getenv("WEEX_PASSPHRASE")
        self.risk_usdt = risk_usdt
        self.sandbox = sandbox

        self.exchange = self._init_exchange()
        self.markets = None

    def _init_exchange(self) -> Optional[ccxt.Exchange]:
        """Initializes WEEX via ccxt. Falls back to dry-run mode if credentials are missing."""
        if not (self.api_key and self.secret and self.passphrase):
            print("[!] Warning: WEEX API credentials missing in .env. Operating in DRY-RUN mode.")
            return None

        if not hasattr(ccxt, "weex"):
            raise ValueError("Installed ccxt version does not support 'weex'. Upgrade via 'pip install ccxt --upgrade'")

        exchange = ccxt.weex({
            "apiKey": self.api_key,
            "secret": self.secret,
            "password": self.passphrase,
            "enableRateLimit": True,
            "options": {
                "defaultType": "swap",  # Perpetual / Futures swap contracts
            }
        })

        if self.sandbox:
            exchange.set_sandbox_mode(True)
            print("[*] WEEX running in SANDBOX / DEMO mode.")
        else:
            print("[*] WEEX running in LIVE PRODUCTION mode.")

        return exchange

    def load_markets(self):
        """Loads and caches exchange market precision and lot sizes."""
        if self.exchange and not self.markets:
            print("[*] Fetching market metadata from WEEX...")
            self.markets = self.exchange.load_markets()

    @staticmethod
    def resolve_symbol(pair: str) -> str:
        """
        Converts parsed strings (e.g., 'KASUUSDT', 'APTUSDT', 'BTCUSDT')
        into CCXT standard swap notation: 'KASU/USDT:USDT', 'APT/USDT:USDT'
        """
        clean = pair.upper().replace("/", "").replace("-", "").strip()
        if clean.endswith("USDT"):
            base = clean[:-4]
            return f"{base}/USDT:USDT"
        return f"{clean}/USDT:USDT"

    def calculate_amount(self, symbol: str, reference_price: float) -> float:
        """Calculates order contract quantity based on USDT allocation and exchange precision."""
        if not self.exchange or not self.markets or symbol not in self.markets:
            return 1.0

        if reference_price <= 0:
            return 1.0

        raw_qty = self.risk_usdt / reference_price
        formatted_qty = self.exchange.amount_to_precision(symbol, raw_qty)
        return float(formatted_qty)

    def fetch_positions(self, symbols: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """Fetches active positions (supported in both live and sandbox)."""
        if not self.exchange:
            return []
        try:
            return self.exchange.fetch_positions(symbols)
        except Exception as e:
            print(f"[!] Error fetching positions: {e}")
            return []

    def fetch_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Fetches open orders.
        Note: Blocked by CCXT in sandbox mode for WEEX, but fully supported in live.
        """
        if not self.exchange:
            return []

        if self.sandbox:
            print("[!] Note: WEEX sandbox mode does not support fetch_open_orders via API.")
            return []

        try:
            return self.exchange.fetch_open_orders(symbol)
        except Exception as e:
            print(f"[!] Error fetching open orders: {e}")
            return []

    def execute_signal(self, signal: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validates signal parameters and executes the order on WEEX.
        Attaches native TP/SL parameters directly to the entry order.
        """
        raw_pair = signal["pair"]
        action = signal["action"].upper()
        entry_val = signal.get("entry", "CMP")
        tp_val = signal.get("tp")
        sl_val = signal.get("sl")

        symbol = self.resolve_symbol(raw_pair)
        side = "buy" if action == "LONG" else "sell"
        position_side = "LONG" if action == "LONG" else "SHORT"

        is_market = isinstance(entry_val, str) and entry_val.strip().upper() == "CMP"
        order_type = "market" if is_market else "limit"

        has_tp = tp_val is not None and not (isinstance(tp_val, str) and tp_val.strip().upper() == "OPEN")
        has_sl = sl_val is not None

        # --- Live / Sandbox Exchange Execution ---
        self.load_markets()

        if is_market:
            ticker = self.exchange.fetch_ticker(symbol)
            ref_price = float(ticker["last"])
            limit_price = None
        else:
            ref_price = float(entry_val)
            limit_price = float(self.exchange.price_to_precision(symbol, ref_price))

        amount = self.calculate_amount(symbol, ref_price)

        # WEEX contract order params
        params: Dict[str, Any] = {
            "positionSide": position_side,
            "timeInForce": "GTC"
        }

        # Attach TP / SL
        if has_tp:
            formatted_tp = str(self.exchange.price_to_precision(symbol, float(tp_val)))
            params["tpTriggerPrice"] = formatted_tp
            params["TpWorkingType"] = "CONTRACT_PRICE"

        if has_sl:
            formatted_sl = str(self.exchange.price_to_precision(symbol, float(sl_val)))
            params["slTriggerPrice"] = formatted_sl
            params["SlWorkingType"] = "MARK_PRICE"

        print(f"[*] Placing {order_type.upper()} {side.upper()} order for {amount} {symbol}...")
        if has_tp or has_sl:
            print(f"    Attached TP: {params.get('tpTriggerPrice')} | SL: {params.get('slTriggerPrice')}")

        order_result = self.exchange.create_order(
            symbol=symbol,
            type=order_type,
            side=side,
            amount=amount,
            price=limit_price,
            params=params
        )

        return {
            "status": "success",
            "order_id": order_result.get("id"),
            "symbol": symbol,
            "type": order_type,
            "side": side,
            "amount": amount,
            "price": limit_price or ref_price,
            "tp": float(tp_val) if has_tp else None,
            "sl": float(sl_val) if has_sl else None,
            "exchange_response": order_result,
            "raw_signal": signal
        }


if __name__ == "__main__":
    # To run on live: service = TradeExecutionService(risk_usdt=20.0, sandbox=False)
    service = TradeExecutionService(risk_usdt=20.0, sandbox=True)
    test_signal = {
        "pair": "BTCUSDT",
        "action": "LONG",
        "entry": "CMP",
        "tp": 84900,
        "sl": 84100,
        "message_id": "test123"
    }

    result = service.execute_signal(test_signal)
    print(result)

    # 1. Inspect positions
    positions = service.fetch_positions(["BTC/USDT:USDT"])
    print("\n--- Active Positions ---")
    for pos in positions:
        print(f"Symbol: {pos['symbol']}, Side: {pos['side']}, Contracts: {pos['contracts']}, Entry: {pos['entryPrice']}")


    # 2. Inspect open orders (safe in sandbox, functional in live)
    open_orders = service.fetch_open_orders("BTC/USDT:USDT")
    print(f"\n--- Open Orders ({len(open_orders)}) ---")
    print(open_orders)

