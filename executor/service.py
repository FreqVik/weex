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

    def __init__(self, risk_usdt: float = 20.0, sandbox: Optional[bool] = None):
        self.api_key = os.getenv("WEEX_API_KEY")
        self.secret = os.getenv("WEEX_SECRET_KEY")
        self.passphrase = os.getenv("WEEX_PASSPHRASE")
        self.risk_usdt = risk_usdt

        if not (self.api_key and self.secret and self.passphrase):
            raise ValueError("WEEX API credentials missing in .env (WEEX_API_KEY, WEEX_SECRET_KEY, WEEX_PASSPHRASE).")

        # Pull from env if not explicitly passed (defaults to True for safety)
        if sandbox is None:
            self.sandbox = os.getenv("WEEX_SANDBOX", "true").strip().lower() in ("true", "1", "yes")
        else:
            self.sandbox = sandbox

        self.exchange = self._init_exchange()
        self.markets = None

    def _init_exchange(self) -> ccxt.Exchange:
        """Initializes WEEX via ccxt."""
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
            print("[*] WEEX Initialized in SANDBOX (Demo) mode.")
        else:
            print("[*] WEEX Initialized in LIVE mode.")

        return exchange

    def load_markets(self):
        """Loads and caches exchange market precision and lot sizes."""
        if not self.markets:
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
        """
        Calculates order contract quantity based on USDT allocation and exchange precision.
        Raises ValueError if calculation or limits fail. Never falls back to a default value.
        """
        if not self.markets or symbol not in self.markets:
            raise ValueError(f"Market metadata not loaded or symbol '{symbol}' not found on WEEX.")

        if reference_price <= 0:
            raise ValueError(f"Invalid reference price ({reference_price}) for symbol '{symbol}'.")

        raw_qty = self.risk_usdt / reference_price
        formatted_qty_str = self.exchange.amount_to_precision(symbol, raw_qty)

        try:
            qty = float(formatted_qty_str)
        except (ValueError, TypeError):
            raise ValueError(f"Failed to parse formatted quantity '{formatted_qty_str}' for symbol '{symbol}'.")

        if qty <= 0:
            raise ValueError(
                f"Calculated amount {qty} is too small for precision of '{symbol}'. "
                f"Risk allocation ({self.risk_usdt} USDT) at price ({reference_price}) rounds to zero."
            )

        # Validate against exchange minimum amount limits if defined
        market_limits = self.markets[symbol].get("limits", {}).get("amount", {})
        min_amount = market_limits.get("min")
        if min_amount is not None and qty < min_amount:
            raise ValueError(
                f"Calculated amount ({qty}) is below exchange minimum order size ({min_amount}) for '{symbol}'."
            )

        return qty

    def fetch_positions(self, symbols: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """Fetches open positions (works on both live and sandbox)."""
        try:
            return self.exchange.fetch_positions(symbols)
        except Exception as e:
            print(f"[!] Error fetching positions: {e}")
            return []

    def execute_signal(self, signal: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validates signal parameters and executes the order on WEEX.
        Attaches native TP/SL parameters directly to the entry order payload.
        Skips signal cleanly if amount calculation or market loading fails.
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

        try:
            self.load_markets()

            if is_market:
                ticker = self.exchange.fetch_ticker(symbol)
                ref_price = float(ticker["last"])
                limit_price = None
            else:
                ref_price = float(entry_val)
                limit_price = float(self.exchange.price_to_precision(symbol, ref_price))

            # Strictly calculate amount or abort if invalid
            amount = self.calculate_amount(symbol, ref_price)

        except Exception as e:
            print(f"[!] Signal rejected/skipped for {symbol}: {e}")
            return {
                "status": "skipped",
                "symbol": symbol,
                "reason": str(e),
                "raw_signal": signal
            }

        # Build WEEX native contract payload
        params: Dict[str, Any] = {
            "positionSide": position_side,
            "timeInForce": "GTC"
        }

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

        try:
            order_result = self.exchange.create_order(
                symbol=symbol,
                type=order_type,
                side=side,
                amount=amount,
                price=limit_price,
                params=params
            )
        except Exception as e:
            print(f"[!] Order creation failed on exchange: {e}")
            return {
                "status": "failed",
                "symbol": symbol,
                "reason": str(e),
                "raw_signal": signal
            }

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

"""
if __name__ == "__main__":
    service = TradeExecutionService(risk_usdt=20.0, sandbox=True)
    test_signal = {
        "pair": "DOGEUSDT",
        "action": "LONG",
        "entry": "CMP",
        "tp": 0.093,
        "sl": 0.0919,
        "message_id": "test123"
    }
    result = service.execute_signal(test_signal)
    print(result)
"""