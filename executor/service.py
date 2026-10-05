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
    - Position size sized to lose target risk_usdt on SL.
    - Strictly clamped to a 25 USDT wallet limit to prevent -1191 margin rejections.
    """

    def __init__(self, risk_usdt: float = 10.0, max_wallet_limit: float = 25.0, sandbox: Optional[bool] = None):
        self.api_key = os.getenv("WEEX_API_KEY")
        self.secret = os.getenv("WEEX_SECRET_KEY")
        self.passphrase = os.getenv("WEEX_PASSPHRASE")
        self.risk_usdt = risk_usdt
        self.max_wallet_limit = max_wallet_limit  # Caps wallet margin baseline to 25 USDT for safety

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
        Converts parsed strings (e.g., 'UNIUSDT', 'JTOUSDT', 'ZAMAUSDT')
        into CCXT standard swap notation: 'UNI/USDT:USDT', 'JTO/USDT:USDT'
        """
        clean = pair.upper().replace("/", "").replace("-", "").strip()
        if clean.endswith("USDT"):
            base = clean[:-4]
            return f"{base}/USDT:USDT"
        return f"{clean}/USDT:USDT"

    def calculate_safe_leverage(self, ref_price: float, sl_price: Optional[float]) -> int:
        """
        Calculates safe leverage dynamically so liquidation is safely
        behind the Stop Loss, keeping initial margin within small wallet bounds.
        Clamped to 20x max to support altcoin tier restrictions.
        """
        if not sl_price or sl_price <= 0:
            return 10  # Fallback leverage if no SL is provided

        sl_distance_pct = abs(ref_price - sl_price) / ref_price
        if sl_distance_pct <= 0:
            return 10

        # Theoretical liquidation distance is ~1/leverage.
        # Apply a 30% safety cushion (0.70 / sl_distance_pct).
        calculated_leverage = int(0.70 / sl_distance_pct)

        # Clamp between 3x (wide swings) and 20x (capped for altcoin tiers)
        return max(3, min(calculated_leverage, 20))

    def set_leverage(self, symbol: str, leverage: int) -> None:
        """
        Sets the leverage on WEEX for the specified symbol.
        Provides both isolatedLongLeverage and isolatedShortLeverage
        to prevent WEEX error code -1141.
        """
        try:
            params = {
                "isolatedLongLeverage": leverage,
                "isolatedShortLeverage": leverage,
            }
            self.exchange.set_leverage(leverage, symbol, params=params)
            print(f"[*] Set leverage to {leverage}x for {symbol}")
        except Exception as e:
            try:
                self.exchange.set_leverage(leverage, symbol)
                print(f"[*] Set leverage to {leverage}x for {symbol} (fallback)")
            except Exception as inner_e:
                print(f"[!] Warning setting leverage for {symbol} ({leverage}x): {inner_e}")

    def calculate_amount(self, symbol: str, reference_price: float, sl_price: Optional[float] = None, leverage: int = 20) -> float:
        """
        Calculates order contract quantity so that:
        amount * |reference_price - sl_price| == self.risk_usdt ($10)
        AND caps quantity to a maximum wallet balance assumption of 25 USDT.
        """
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

        # 2. Strict 25 USDT Safety Balance Guard
        try:
            balance_info = self.exchange.fetch_balance()
            free_balance = float(
                balance_info.get("USDT", {}).get("free") 
                or balance_info.get("SUSDT", {}).get("free") 
                or 0.0
            )

            # Cap effective balance to 25 USDT maximum for extra safety cushion
            effective_wallet = min(free_balance if free_balance > 0 else self.max_wallet_limit, self.max_wallet_limit)

            # 85% utilization multiplier allows buffer for taker fees & execution slippage
            max_affordable_notional = effective_wallet * leverage * 0.85
            max_affordable_qty = max_affordable_notional / reference_price

            if raw_qty > max_affordable_qty:
                target_notional = raw_qty * reference_price
                print(
                    f"[!] Target notional (${target_notional:.2f}) exceeds the 25 USDT safety ceiling (${max_affordable_notional:.2f}).\n"
                    f"    Clamping size from {raw_qty:.2f} down to {max_affordable_qty:.2f} {symbol}."
                )
                raw_qty = max_affordable_qty

        except Exception as e:
            # Fallback if fetch_balance network fails: use hardcoded 25 USDT threshold
            max_affordable_notional = self.max_wallet_limit * leverage * 0.85
            max_affordable_qty = max_affordable_notional / reference_price
            if raw_qty > max_affordable_qty:
                raw_qty = max_affordable_qty

        # 3. Format according to exchange step size & precision
        formatted_qty_str = self.exchange.amount_to_precision(symbol, raw_qty)

        try:
            qty = float(formatted_qty_str)
        except (ValueError, TypeError):
            raise ValueError(f"Failed to parse formatted quantity '{formatted_qty_str}' for symbol '{symbol}'.")

        if qty <= 0:
            raise ValueError(f"Calculated amount {qty} is too small for precision of '{symbol}'.")

        # 4. Enforce minimum order threshold
        market = self.markets[symbol]
        market_limits = market.get("limits", {}).get("amount", {})
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
        Sets dynamic leverage and sizes strictly to risk_usdt on SL,
        clamped to the 25 USDT balance ceiling.
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
        has_sl = sl_val is not None and not (isinstance(sl_val, str) and sl_val.strip().upper() == "OPEN")

        try:
            self.load_markets()

            if is_market:
                ticker = self.exchange.fetch_ticker(symbol)
                ref_price = float(ticker["last"])
                limit_price = None
            else:
                ref_price = float(entry_val)
                limit_price = float(self.exchange.price_to_precision(symbol, ref_price))

            sl_price_num = float(sl_val) if has_sl else None

            # 1. Compute and set dynamic leverage safely
            safe_leverage = self.calculate_safe_leverage(ref_price, sl_price_num)
            self.set_leverage(symbol, safe_leverage)

            # 2. Calculate quantity strictly sized to risk_usdt (clamped by 25 USDT limit)
            amount = self.calculate_amount(symbol, ref_price, sl_price_num, leverage=safe_leverage)

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
        }

        # timeInForce must ONLY be passed for limit orders
        if not is_market:
            params["timeInForce"] = "GTC"

        if has_tp:
            formatted_tp = str(self.exchange.price_to_precision(symbol, float(tp_val)))
            params["tpTriggerPrice"] = formatted_tp
            params["TpWorkingType"] = "CONTRACT_PRICE"

        if has_sl:
            formatted_sl = str(self.exchange.price_to_precision(symbol, float(sl_val)))
            params["slTriggerPrice"] = formatted_sl
            params["SlWorkingType"] = "MARK_PRICE"

        print(f"[*] Placing {order_type.upper()} {side.upper()} order for {amount} {symbol} ({safe_leverage}x leverage)...")
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
            "leverage": safe_leverage,
            "price": limit_price or ref_price,
            "tp": float(tp_val) if has_tp else None,
            "sl": float(sl_val) if has_sl else None,
            "exchange_response": order_result,
            "raw_signal": signal
        }


if __name__ == "__main__":
    # Test with UNIUSDT using the 25 USDT assumption
    service = TradeExecutionService(risk_usdt=10.0, max_wallet_limit=25.0, sandbox=False)
    test_signal = {
        "pair": "UNIUSDT",
        "action": "SHORT",
        "entry": "CMP",
        "tp": 8.149,
        "sl": 9.303,
        "message_id": "test_uni_clamped"
    }
    result = service.execute_signal(test_signal)
    print(result)