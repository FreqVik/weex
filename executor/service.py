# executor/service.py
import os
from typing import Optional, Dict, Any, List, Set
from dotenv import load_dotenv
import ccxt

load_dotenv()


class TradeExecutionService:
    """
    Executes trades on WEEX based on parsed Telegram signals:
    - Entry == 'CMP' -> Market Order
    - Entry == float -> Limit Order
    - Enforces an automated 20% wallet profit target ($3.90 USDT) per trade.
    - Sized to risk target risk_usdt on SL, clamped to a 19.5 USDT wallet ceiling.
    - Dynamic safe leverage formula ensuring a substantial liquidation cushion beyond SL.
    - Pre-filters pairs against WEEX's API Trading Symbols whitelist to prevent -1058 errors.
    """

    def __init__(
        self,
        risk_usdt: float = 10.0,
        max_wallet_limit: float = 19.5,
        sandbox: Optional[bool] = None,
        enforce_fixed_tp: bool = True,
        target_wallet_tp_pct: float = 0.20,  # 20% wallet profit target ($3.90 USDT)
    ):
        self.api_key = os.getenv("WEEX_API_KEY")
        self.secret = os.getenv("WEEX_SECRET_KEY")
        self.passphrase = os.getenv("WEEX_PASSPHRASE")
        self.risk_usdt = risk_usdt
        self.max_wallet_limit = max_wallet_limit
        self.enforce_fixed_tp = enforce_fixed_tp
        self.target_wallet_tp_pct = target_wallet_tp_pct

        if not (self.api_key and self.secret and self.passphrase):
            raise ValueError("WEEX API credentials missing in .env (WEEX_API_KEY, WEEX_SECRET_KEY, WEEX_PASSPHRASE).")

        if sandbox is None:
            self.sandbox = os.getenv("WEEX_SANDBOX", "true").strip().lower() in ("true", "1", "yes")
        else:
            self.sandbox = sandbox

        self.exchange = self._init_exchange()
        self.markets = None
        self.api_supported_symbols: Set[str] = set()

    def _init_exchange(self) -> ccxt.Exchange:
        """Initializes WEEX via CCXT."""
        if not hasattr(ccxt, "weex"):
            raise ValueError("Installed ccxt version does not support 'weex'. Upgrade via 'pip install ccxt --upgrade'")

        exchange = ccxt.weex({
            "apiKey": self.api_key,
            "secret": self.secret,
            "password": self.passphrase,
            "enableRateLimit": True,
            "options": {
                "defaultType": "swap",
            }
        })

        if self.sandbox:
            exchange.set_sandbox_mode(True)
            print("[*] WEEX Initialized in SANDBOX (Demo) mode.")
        else:
            print("[*] WEEX Initialized in LIVE mode.")

        return exchange

    def load_markets(self):
        """Loads and caches market precision and fetches API-supported trading symbols."""
        if not self.markets:
            print("[*] Fetching market metadata from WEEX...")
            self.markets = self.exchange.load_markets()

            try:
                # Query WEEX endpoint: GET /capi/v3/market/apiTradingSymbols
                eligible = self.exchange.public_get_capi_v3_market_apitradingsymbols()
                if isinstance(eligible, list):
                    self.api_supported_symbols = {
                        f"{s[:-4]}/USDT:USDT" for s in eligible if isinstance(s, str) and s.endswith("USDT")
                    }
                    print(f"[*] Loaded {len(self.api_supported_symbols)} API-whitelisted futures symbols.")
            except Exception as e:
                print(f"[!] Note: Could not fetch apiTradingSymbols whitelist ({e}). Falling back to markets list.")

    @staticmethod
    def resolve_symbol(pair: str) -> str:
        clean = pair.upper().replace("/", "").replace("-", "").strip()
        if clean.endswith("USDT"):
            base = clean[:-4]
            return f"{base}/USDT:USDT"
        return f"{clean}/USDT:USDT"

    def calculate_safe_leverage(self, ref_price: float, sl_price: Optional[float], mmr: float = 0.01) -> int:
        """
        Dynamically calculates safe leverage ensuring liquidation is placed
        at least 1.8x the SL distance away. Clamped between 2x and 15x.
        """
        if not sl_price or sl_price <= 0:
            return 10

        sl_distance_pct = abs(ref_price - sl_price) / ref_price
        if sl_distance_pct <= 0:
            return 10

        target_liq_distance = (1.8 * sl_distance_pct) + mmr
        calculated_leverage = int(1.0 / target_liq_distance)

        return max(2, min(calculated_leverage, 15))

    def set_leverage(self, symbol: str, leverage: int) -> None:
        """Sets leverage while passing isolatedLongLeverage/isolatedShortLeverage to satisfy WEEX."""
        try:
            params = {
                "marginMode": "isolated",
                "isolatedLongLeverage": str(leverage),
                "isolatedShortLeverage": str(leverage),
            }
            self.exchange.set_leverage(leverage, symbol, params=params)
            print(f"[*] Set leverage to {leverage}x for {symbol}")
        except Exception as e:
            try:
                self.exchange.set_leverage(leverage, symbol)
                print(f"[*] Set leverage to {leverage}x for {symbol} (fallback)")
            except Exception as inner_e:
                print(f"[!] Warning setting leverage for {symbol} ({leverage}x): {inner_e}")

    def calculate_amount(self, symbol: str, reference_price: float, sl_price: Optional[float] = None, leverage: int = 10) -> float:
        """Sizes position based on risk_usdt, strictly clamped by the 19.5 USDT safety balance ceiling."""
        if not self.markets or symbol not in self.markets:
            raise ValueError(f"Market metadata not loaded or symbol '{symbol}' not found on WEEX.")

        if reference_price <= 0:
            raise ValueError(f"Invalid reference price ({reference_price}) for symbol '{symbol}'.")

        # 1. Base quantity calculated from SL distance
        if sl_price is not None and sl_price > 0:
            price_distance = abs(reference_price - sl_price)
            if price_distance <= 0:
                raise ValueError(f"Entry ({reference_price}) and SL ({sl_price}) cannot be identical.")
            raw_qty = self.risk_usdt / price_distance
        else:
            raw_qty = self.risk_usdt / reference_price

        # 2. Safety Balance Guard (19.5 USDT max baseline, 85% utilization multiplier)
        try:
            balance_info = self.exchange.fetch_balance()
            free_balance = float(
                balance_info.get("USDT", {}).get("free")
                or balance_info.get("SUSDT", {}).get("free")
                or 0.0
            )
            effective_wallet = min(free_balance if free_balance > 0 else self.max_wallet_limit, self.max_wallet_limit)
            max_affordable_notional = effective_wallet * leverage * 0.85
            max_affordable_qty = max_affordable_notional / reference_price

            if raw_qty > max_affordable_qty:
                target_notional = raw_qty * reference_price
                print(
                    f"[!] Target notional (${target_notional:.2f}) exceeds the {self.max_wallet_limit} USDT safety ceiling (${max_affordable_notional:.2f}).\n"
                    f"    Clamping size from {raw_qty:.2f} down to {max_affordable_qty:.2f} {symbol}."
                )
                raw_qty = max_affordable_qty
        except Exception:
            max_affordable_notional = self.max_wallet_limit * leverage * 0.85
            max_affordable_qty = max_affordable_notional / reference_price
            if raw_qty > max_affordable_qty:
                raw_qty = max_affordable_qty

        # 3. Precision format & limits check
        formatted_qty_str = self.exchange.amount_to_precision(symbol, raw_qty)
        qty = float(formatted_qty_str)

        if qty <= 0:
            raise ValueError(f"Calculated amount {qty} is too small for precision of '{symbol}'.")

        market = self.markets[symbol]
        min_amount = market.get("limits", {}).get("amount", {}).get("min")
        if min_amount is not None and qty < min_amount:
            raise ValueError(f"Calculated amount ({qty}) is below exchange minimum order size ({min_amount}) for '{symbol}'.")

        return qty

    def execute_signal(self, signal: Dict[str, Any]) -> Dict[str, Any]:
        """Validates signal parameters and executes order with native 20% TP and SL attached."""
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
        has_sl = sl_val is not None and not (isinstance(sl_val, str) and sl_val.strip().upper() == "OPEN")

        try:
            self.load_markets()

            # Pre-filter against API whitelist to avoid -1058 errors
            if self.api_supported_symbols and symbol not in self.api_supported_symbols:
                raise ValueError(f"{symbol} is UI-only on WEEX and not enabled for automated API trading.")

            if is_market:
                ticker = self.exchange.fetch_ticker(symbol)
                ref_price = float(ticker["last"])
                limit_price = None
            else:
                ref_price = float(entry_val)
                limit_price = float(self.exchange.price_to_precision(symbol, ref_price))

            sl_price_num = float(sl_val) if has_sl else None
            safe_leverage = self.calculate_safe_leverage(ref_price, sl_price_num)
            self.set_leverage(symbol, safe_leverage)

            amount = self.calculate_amount(symbol, ref_price, sl_price_num, leverage=safe_leverage)

        except Exception as e:
            print(f"[!] Signal rejected/skipped for {symbol}: {e}")
            return {
                "status": "skipped",
                "symbol": symbol,
                "reason": str(e),
                "raw_signal": signal
            }

        params: Dict[str, Any] = {
            "positionSide": position_side,
        }

        if not is_market:
            params["timeInForce"] = "GTC"

        # -------------------------------------------------------------
        # Enforced 20% Wallet Target Profit Rule:
        # Desired Gain = $19.50 * 20% = $3.90 USDT
        # -------------------------------------------------------------
        tp_price_target: Optional[float] = None
        if self.enforce_fixed_tp or tp_val is None or (isinstance(tp_val, str) and tp_val.strip().upper() == "OPEN"):
            target_profit_usdt = self.max_wallet_limit * self.target_wallet_tp_pct  # $3.90 USDT
            price_delta_for_tp = target_profit_usdt / amount

            if action == "LONG":
                tp_price_target = ref_price + price_delta_for_tp
            else:
                tp_price_target = ref_price - price_delta_for_tp
        else:
            tp_price_target = float(tp_val)

        if tp_price_target and tp_price_target > 0:
            formatted_tp = str(self.exchange.price_to_precision(symbol, tp_price_target))
            params["tpTriggerPrice"] = formatted_tp
            params["TpWorkingType"] = "CONTRACT_PRICE"

        if has_sl:
            formatted_sl = str(self.exchange.price_to_precision(symbol, float(sl_val)))
            params["slTriggerPrice"] = formatted_sl
            params["SlWorkingType"] = "MARK_PRICE"

        print(f"[*] Placing {order_type.upper()} {side.upper()} order for {amount} {symbol} ({safe_leverage}x leverage)...")
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
            "leverage": safe_leverage,
            "price": limit_price or ref_price,
            "tp": float(params.get("tpTriggerPrice")) if "tpTriggerPrice" in params else None,
            "sl": float(params.get("slTriggerPrice")) if "slTriggerPrice" in params else None,
            "exchange_response": order_result,
            "raw_signal": signal
        }

"""
if __name__ == "__main__":
    service = TradeExecutionService(
        risk_usdt=10.0,
        max_wallet_limit=19.5,
        sandbox=True,
        enforce_fixed_tp=True,
        target_wallet_tp_pct=0.20  # +20% ($3.90 gain)
    )
    test_signal = {
        "pair": "DOGEUSDT",
        "action": "SHORT",
        "entry": "CMP",
        "tp": 0.07600,
        "sl": 0.083,
        "message_id": "test_doge_automated_20pct"
    }
    result = service.execute_signal(test_signal)
    print(result)
"""