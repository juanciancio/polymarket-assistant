"""Live trading layer for Polymarket.

Mirrors paper_trading.py logic exactly: same cooldown (45s), burst limit
(3/5min), VWAP filter, TP/SL/trailing thresholds. Paper trading always
runs in parallel and is never affected by exceptions here.

Requires:
    pip install py-clob-client python-dotenv

Environment variables (from .env):
    POLYMARKET_KEY, POLYMARKET_API_KEY, POLYMARKET_API_SECRET,
    POLYMARKET_API_PASSPHRASE, POLYMARKET_FUNDER,
    LIVE_TRADING (default: false), MAX_POSITION_SIZE (default: 5),
    MAX_DAILY_LOSS (default: 30), SLIPPAGE_TOLERANCE (default: 0.02)
"""

import asyncio
import os
import json
import time
from datetime import datetime, timezone

from dotenv import load_dotenv


def calc_volatility(trades: list, mid: float) -> float:
    """Price range % over last 60 seconds of trades.
    Returns -1.0 if insufficient data."""
    try:
        now    = time.time()
        recent = [t for t in trades if t["t"] >= now - 60]
        if len(recent) < 5:
            return -1.0
        prices = [t["price"] for t in recent]
        m      = (max(prices) + min(prices)) / 2
        if m == 0:
            return -1.0
        return (max(prices) - min(prices)) / m * 100
    except Exception:
        return -1.0

load_dotenv()

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.constants import POLYGON
    from py_clob_client.clob_types import ApiCreds, OrderType, MarketOrderArgs
    from py_clob_client.order_builder.constants import BUY, SELL
    _CLOB_AVAILABLE = True
except ImportError:
    _CLOB_AVAILABLE = False

import config

# ── Safety hardcaps (non-negotiable, override .env) ──────────────────────────
_MAX_POSITION_HARDCAP = 50.0
_SLIPPAGE_HARDCAP     = 0.05

# ── Timing mirrors (must match paper_trading.py) ─────────────────────────────
_COOLDOWN_SECS = 10
_BURST_WINDOW  = 300
_BURST_MAX     = 5

WIN_STATUSES    = {"WIN_TP", "WIN_FULL", "WIN_TRAIL"}
LOSS_STATUSES   = {"LOSS_SL", "LOSS_FULL"}
CLOSED_STATUSES = WIN_STATUSES | LOSS_STATUSES

# ── File paths ────────────────────────────────────────────────────────────────
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_TRADES_FILE = os.path.join(_ROOT, "live_trades.json")
_ERRORS_LOG          = os.path.join(_ROOT, "errors_log.json")


class LiveTrader:
    def __init__(self, filepath: str = _DEFAULT_TRADES_FILE):
        # ── Constants ──────────────────────────────────────────────────
        self.live_trading = os.getenv("LIVE_TRADING", "false") == "true"
        self.max_position_size = min(
            float(os.getenv("MAX_POSITION_SIZE", "5")),
            _MAX_POSITION_HARDCAP,
        )
        self.daily_loss_limit  = float(os.getenv("MAX_DAILY_LOSS", "30"))
        self.slippage_tolerance = min(
            float(os.getenv("SLIPPAGE_TOLERANCE", "0.02")),
            _SLIPPAGE_HARDCAP,
        )

        # ── Session state ───────────────────────────────────────────────
        self.daily_pnl: float                  = 0.0
        self.last_close_timestamp: datetime | None = None
        self.recent_trades: list[datetime]     = []
        self.last_clob_call: float             = 0.0  # epoch of last CLOB HTTP request
        self._opening_position: bool           = False  # guard against concurrent opens

        # ── Persistent data ─────────────────────────────────────────────
        self._file = filepath
        self._data = self._load()
        self._data["summary"]["daily_loss_limit"] = self.daily_loss_limit

        # O(1) open-position cache — must be after _data is loaded
        self._open_position: dict | None = next(
            (p for p in self._data["positions"] if p["status"] == "OPEN"), None
        )

        # Recover today's P&L from existing records
        today = datetime.now(timezone.utc).date()
        self.daily_pnl = sum(
            p.get("pnl_usd") or 0
            for p in self._data.get("positions", [])
            if p.get("timestamp_close")
            and datetime.fromisoformat(p["timestamp_close"]).date() == today
            and p.get("pnl_usd") is not None
        )

        # ── Polymarket client ───────────────────────────────────────────
        if not _CLOB_AVAILABLE:
            print("  [LiveTrader] py-clob-client not installed — live trading disabled")
            self.live_trading = False
            self.client = None
            return

        try:
            self.client = ClobClient(
                host="https://clob.polymarket.com",
                key=os.getenv("POLYMARKET_KEY", ""),
                chain_id=137,
                signature_type=1,           # Magic Link — obligatorio
                funder=os.getenv("POLYMARKET_FUNDER", ""),
            )
            self.client.set_api_creds(self.client.create_or_derive_api_creds())
            print("OK:", self.client.get_api_keys())

        except Exception as e:
            print(f"  [LiveTrader] client init failed: {e}")
            self.live_trading = False
            self.client = None

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load(self) -> dict:
        if os.path.exists(self._file):
            try:
                with open(self._file, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {
            "summary": {
                "total_trades":       0,
                "wins":               0,
                "losses":             0,
                "open":               0,
                "close_failed":       0,
                "win_rate":           0.0,
                "total_pnl_usd":      0.0,
                "daily_pnl":          0.0,
                "daily_loss_limit":   30.0,
                "fok_canceled_count": 0,
                "avg_slippage":       0.0,
            },
            "positions": [],
        }

    def _save(self):
        with open(self._file, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)

    def _recalc_summary(self):
        positions = self._data["positions"]
        closed    = [p for p in positions if p["status"] in CLOSED_STATUSES]
        opens     = [p for p in positions if p["status"] == "OPEN"]
        failed    = [p for p in positions if p["status"] == "CLOSE_FAILED"]
        n_wins    = sum(1 for p in closed if p["status"] in WIN_STATUSES)
        n_closed  = len(closed)
        total_pnl = sum(p["pnl_usd"] for p in closed if p["pnl_usd"] is not None)

        slippages = [
            p["slippage_applied"]
            for p in positions
            if p.get("slippage_applied") is not None
        ]
        avg_slip = (sum(slippages) / len(slippages)) if slippages else 0.0

        self._data["summary"].update({
            "total_trades":     n_closed,
            "wins":             n_wins,
            "losses":           n_closed - n_wins,
            "open":             len(opens),
            "close_failed":     len(failed),
            "win_rate":         (n_wins / n_closed * 100) if n_closed else 0.0,
            "total_pnl_usd":    total_pnl,
            "daily_pnl":        self.daily_pnl,
            "daily_loss_limit": self.daily_loss_limit,
            "avg_slippage":     avg_slip,
        })

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def summary(self) -> dict:
        return self._data["summary"]

    @property
    def current_open_position(self) -> dict | None:
        """Return the single OPEN position, or None. O(1) — cache updated on open/close."""
        return self._open_position

    @property
    def cooldown_remaining(self) -> int:
        if self.last_close_timestamp is None:
            return 0
        elapsed = (datetime.now(timezone.utc) - self.last_close_timestamp).total_seconds()
        return max(0, int(_COOLDOWN_SECS - elapsed))

    @property
    def is_burst_limited(self) -> bool:
        now    = datetime.now(timezone.utc)
        recent = [t for t in self.recent_trades
                  if (now - t).total_seconds() < _BURST_WINDOW]
        return len(recent) >= _BURST_MAX

    @property
    def has_close_failed(self) -> bool:
        return self._data["summary"].get("close_failed", 0) > 0

    # ── API methods ───────────────────────────────────────────────────────────

    def verify_connection(self) -> bool:
        if not self.live_trading or self.client is None:
            return False
        try:
            response = self.client.get_api_keys()
            if response:
                print("✅ Polymarket API conectada correctamente")
                return True
        except Exception as e:
            print(f"❌ Error de conexión: {e}")
            self.live_trading = False
        return False

    async def get_best_price(self, token_id: str) -> float | None:
        try:
            orderbook = await asyncio.to_thread(self.client.get_order_book, token_id)
            asks = orderbook.asks
            if asks:
                return float(min(asks, key=lambda x: x.price).price)
            return None
        except Exception as e:
            self._log_error("get_best_price", e)
            return None

    async def place_buy_order(self, token_id: str, amount_usd: float) -> dict | None:
        """amount_usd = dólares a gastar (self.max_position_size)."""
        try:
            mo     = MarketOrderArgs(token_id=token_id, amount=amount_usd, side=BUY)
            signed = await asyncio.to_thread(self.client.create_market_order, mo)
            await asyncio.sleep(0)   # yield to event loop between CPU-bound calls
            resp   = await asyncio.to_thread(self.client.post_order, signed, OrderType.FOK)
            self.last_clob_call = time.time()
            print(f"[LiveTrader DEBUG] place_buy_order resp={resp}")
            if resp.get("status") == "canceled":
                self._log_error("place_buy_order", f"FOK cancelado token={token_id}")
                self._data["summary"]["fok_canceled_count"] = (
                    self._data["summary"].get("fok_canceled_count", 0) + 1
                )
                await asyncio.to_thread(self._save)
                return None
            return resp
        except Exception as e:
            self._log_error("place_buy_order", str(e))
            return None

    async def _try_sell_once(self, token_id: str, contracts: float) -> dict | None:
        """Single sell attempt — no retries, no delays."""
        try:
            mo     = MarketOrderArgs(token_id=token_id, amount=contracts, side=SELL)
            signed = await asyncio.to_thread(self.client.create_market_order, mo)
            await asyncio.sleep(0)  # yield to event loop
            resp   = await asyncio.to_thread(self.client.post_order, signed, OrderType.FOK)
            self.last_clob_call = time.time()
            if resp.get("status") != "canceled" and resp.get("success"):
                return resp
            return None
        except Exception as e:
            err = str(e)
            if "couldn't be fully filled" in err or "not enough balance" in err:
                return None  # no liquidity — caller will retry
            self._log_error("_try_sell_once", err)
            return None

    async def execute_signal(
        self,
        signal: dict,
        coin: str,
        timeframe: str,
        pm_up_price: float,
        btc_price: float,
        triggered_conditions: list,
        state,
    ) -> dict | None:
        """Open a live position. Identical entry rules to paper_trading.open_position()."""
        print(f"[LiveTrader] execute_signal ENTRADA — live={self.live_trading} coin={coin} tf={timeframe}")

        # 1. Live trading disabled
        if not self.live_trading:
            print("[LiveTrader] ❌ bloqueado: live_trading=False")
            return None

        # Client may be None if init failed
        if self.client is None:
            print("[LiveTrader] ❌ bloqueado: client=None")
            return None

        # Do not open while PM feed is reconnecting — price data may be stale
        if getattr(state, "pm_reconnecting", False) or \
           getattr(state, "reconnection_in_progress", False):
            print("[LiveTrader] ⏭️ PM reconnecting — skipping entry")
            return None

        # 2. One position at a time
        if self.current_open_position is not None:
            print("[LiveTrader] ❌ bloqueado: posición ya abierta")
            return None

        # 3. Daily loss limit
        if self.daily_pnl <= -self.daily_loss_limit:
            print(f"[LiveTrader] ❌ bloqueado: daily loss limit ({self.daily_pnl:.2f})")
            return None

        # 4. Cooldown (45 s)
        remaining = self.cooldown_remaining
        if remaining > 0:
            print(f"[LiveTrader] ❌ bloqueado: cooldown {remaining}s")
            return None

        # 5. Burst limit (3 trades / 5 min)
        now = datetime.now(timezone.utc)
        self.recent_trades = [
            t for t in self.recent_trades
            if (now - t).total_seconds() < _BURST_WINDOW
        ]
        if len(self.recent_trades) >= _BURST_MAX:
            print(f"[LiveTrader] ❌ bloqueado: burst limit {_BURST_MAX}/{_BURST_WINDOW // 60}min")
            return None

        # 6. Direction and detected price
        cl = signal["conviction_level"]
        if cl == "MAX_BULLISH":
            direction      = "LONG"
            detected_price = pm_up_price
        else:
            direction      = "SHORT"
            detected_price = 1.0 - pm_up_price

        # 7. VWAP filter for LONG
        if direction == "LONG":
            vwap_against = any(
                cond[0] == "Price below VWAP"
                for cond in triggered_conditions
            )
            if vwap_against:
                print("[LiveTrader] ❌ bloqueado: LONG con Price below VWAP")
                return None

        # 7b. VWAP filter for SHORT
        if direction == "SHORT":
            vwap_against = any(
                cond[0] == "Price above VWAP"
                for cond in triggered_conditions
            )
            if vwap_against:
                print("[LiveTrader] ❌ bloqueado: SHORT con Price above VWAP")
                return None

        # 8. Price range validation
        if direction == "LONG":
            volatility = self._recent_volatility(state)
            if volatility >= 0 and volatility < 0.10:
                long_upper = 0.85
                vol_label  = f"ULTRA ESTABLE ({volatility:.3f}%)"
            elif volatility >= 0 and volatility < 0.20:
                long_upper = 0.78
                vol_label  = f"MUY ESTABLE ({volatility:.3f}%)"
            else:
                long_upper = 0.75
                vol_label  = f"NORMAL/VOLÁTIL ({volatility:.3f}%)"
            print(f"[LiveTrader] volatilidad 60s: {vol_label} → límite LONG {long_upper}")
            if not (0.55 <= detected_price <= long_upper):
                print(
                    f"[LiveTrader] ❌ bloqueado: LONG precio fuera de rango "
                    f"({detected_price:.4f}, límite={long_upper})"
                )
                return None
        if direction == "SHORT":
            volatility = self._recent_volatility(state)
            if volatility >= 0 and volatility < 0.10:
                short_upper = 0.85
                vol_label   = f"ULTRA ESTABLE ({volatility:.3f}%)"
            elif volatility >= 0 and volatility < 0.20:
                short_upper = 0.70
                vol_label   = f"MUY ESTABLE ({volatility:.3f}%)"
            else:
                short_upper = 0.65
                vol_label   = f"NORMAL/VOLÁTIL ({volatility:.3f}%)"
            print(f"[LiveTrader] volatilidad 60s: {vol_label} → límite SHORT {short_upper}")
            if not (0.35 <= detected_price <= short_upper):
                print(
                    f"[LiveTrader] ❌ bloqueado: SHORT precio fuera de rango "
                    f"({detected_price:.4f}, límite={short_upper})"
                )
                return None

        # 9. Adaptive position sizing based on volatility
        _vol_for_sizing = volatility  # computed in step 8 for both LONG and SHORT

        if _vol_for_sizing >= 0 and _vol_for_sizing < 0.10:
            capital = round(self.max_position_size * 2.0, 2)
            print(
                f"[LiveTrader] 🚀 mercado ULTRA ESTABLE ({_vol_for_sizing:.3f}%) "
                f"— capital máximo: ${capital:.2f}"
            )
        elif _vol_for_sizing >= 0 and _vol_for_sizing < 0.20:
            capital = round(self.max_position_size * 1.5, 2)
            print(
                f"[LiveTrader] 📈 mercado MUY ESTABLE ({_vol_for_sizing:.3f}%) "
                f"— capital aumentado: ${capital:.2f}"
            )
        else:
            capital = self.max_position_size
            print(
                f"[LiveTrader] capital normal: ${capital:.2f} "
                f"(volatilidad {_vol_for_sizing:.3f}%)"
            )

        # Max price with slippage
        max_price = round(detected_price * (1 + self.slippage_tolerance), 4)
        print(f"[LiveTrader] direction={direction} detected={detected_price:.4f} max={max_price:.4f}")

        # 9b. Contract expiry guard — adaptive minimum based on volatility
        if state.market_end_time is not None:
            remaining = (state.market_end_time - datetime.now(timezone.utc)).total_seconds()

            if _vol_for_sizing >= 0 and _vol_for_sizing < 0.10:
                min_secs = 30
                vol_note = f"ULTRA ESTABLE ({_vol_for_sizing:.3f}%)"
            elif _vol_for_sizing >= 0 and _vol_for_sizing < 0.20:
                min_secs = 40
                vol_note = f"MUY ESTABLE ({_vol_for_sizing:.3f}%)"
            elif _vol_for_sizing >= 0 and _vol_for_sizing < 0.50:
                min_secs = 60
                vol_note = f"NORMAL ({_vol_for_sizing:.3f}%)"
            else:
                min_secs = 90
                vol_note = f"VOLÁTIL ({_vol_for_sizing:.3f}%)"

            if remaining < min_secs:
                print(
                    f"[LiveTrader] ⏭️ contract expires in {remaining:.0f}s "
                    f"— skipping entry (min={min_secs}s, market={vol_note})"
                )
                return None
            else:
                print(
                    f"[LiveTrader] ⏱️ {remaining:.0f}s remaining "
                    f"(min={min_secs}s, market={vol_note}) — proceeding"
                )

        # Async race condition guard — set before first await
        if self._opening_position:
            print("[LiveTrader] ❌ bloqueado: apertura en curso")
            return None
        self._opening_position = True

        try:
            # 10. Token ID from state (current dynamic tokens)
            token_id = state.pm_up_id if direction == "LONG" else state.pm_dn_id
            if not token_id:
                print(f"[LiveTrader] ❌ bloqueado: no token_id en state para {coin} {timeframe}")
                self._log_error("execute_signal", f"No token_id en state para {coin} {timeframe}")
                return None
            print(f"[LiveTrader] token_id={token_id[:24]}…")

            # 11. Verify best price
            best = await self.get_best_price(token_id)
            if best is None:
                print(f"[LiveTrader] ❌ bloqueado: cannot fetch best price")
                self._log_error("execute_signal", f"Cannot fetch best price for {token_id}")
                return None
            print(f"[LiveTrader] best_price={best}")

            # 11b. Verify sufficient liquidity before placing buy
            try:
                book      = await asyncio.to_thread(self.client.get_order_book, token_id)
                asks      = book.asks or []
                available = sum(float(a.size) for a in asks[:5])
                needed    = capital / detected_price
                if available < needed:
                    print(
                        f"[LiveTrader] ⏭️ insufficient liquidity for {direction} "
                        f"detected={detected_price:.4f} "
                        f"available={available:.2f} needed={needed:.2f}"
                    )
                    return None
            except Exception as e:
                self._log_error("execute_signal.liquidity_check", str(e))
                return None

            # 12-13. Place FOK market buy — MarketOrderArgs handles sizing from USD amount
            response = await self.place_buy_order(token_id, capital)
            if response is None:
                return None

            # 14. Real fill price — derived from makingAmount / takingAmount
            # Polymarket never returns a "price" field in FOK responses.
            print(f"[LiveTrader DEBUG] buy response completo: {response}")
            contracts         = float(response["takingAmount"])
            actual_fill_price = float(response["makingAmount"]) / contracts
            print(
                f"[LiveTrader DEBUG] fill price derivado: {actual_fill_price:.4f} "
                f"(making={response['makingAmount']} taking={response['takingAmount']})"
            )

            # Guard: fill price must be in safe range (not a near-resolved contract)
            if actual_fill_price < 0.05 or actual_fill_price > 0.95:
                self._log_error(
                    "execute_signal",
                    f"Precio fuera de rango seguro: {actual_fill_price:.4f} — abortando",
                )
                return None

            # Warning: high slippage — track anyway, never abort after a successful buy
            real_slippage = (actual_fill_price - detected_price) / detected_price
            if abs(real_slippage) > 0.05:
                print(
                    f"[LiveTrader] ⚠️ high slippage: {real_slippage * 100:+.2f}% "
                    f"(detected={detected_price:.4f} fill={actual_fill_price:.4f}) "
                    f"— tracking with real fill price"
                )
                self._log_error(
                    "execute_signal",
                    f"High slippage {real_slippage * 100:+.2f}% — "
                    f"detected={detected_price:.4f} fill={actual_fill_price:.4f} "
                    f"— position tracked with real fill price",
                )

            print(f"[LiveTrader DEBUG] contracts reales del response: {contracts}")

            # Guard: contracts truly absurd (> capital / 0.10) — emergency sell then abort
            if contracts > capital / 0.10:
                self._log_error(
                    "execute_signal",
                    f"Contracts absurdos: {contracts:.2f} (max=100) — emergency sell",
                )
                await self.place_sell_order(token_id, contracts, actual_fill_price)
                return None

            tp_target        = round(actual_fill_price + 0.10, 4)
            sl_target        = round(actual_fill_price - 0.15, 4)
            slippage_applied = round(
                (actual_fill_price - detected_price) / detected_price, 4
            )

            # 16. Build and persist position
            pos_id   = len(self._data["positions"]) + 1
            order_id = response.get("id") or response.get("orderID") or ""

            position = {
                "id":                   pos_id,
                "timestamp_open":       now.isoformat(),
                "timestamp_close":      None,
                "coin":                 coin,
                "timeframe":            timeframe,
                "direction":            direction,
                "token_id":             token_id,
                "entry_price_detected": detected_price,
                "entry_price_max":      max_price,
                "entry_price_real":     actual_fill_price,
                "slippage_applied":     slippage_applied,
                "exit_price_detected":  None,
                "exit_price_real":      None,
                "entry_btc_price":      btc_price,
                "exit_btc_price":       None,
                "capital":              capital,
                "contracts":            contracts,
                "tp_target":            tp_target,
                "sl_target":            sl_target,
                "highest_price":        actual_fill_price,
                "ticks_open":           0,
                "pnl_usd":              None,
                "status":               "OPEN",
                "order_id":             order_id,
                "close_order_id":       None,
                "score":                signal.get("score", 0),
                "conviction_level":     cl,
                "triggered_conditions": triggered_conditions,
            }

            self._data["positions"].append(position)
            self._recalc_summary()
            self._open_position = position
            await asyncio.to_thread(self._save)
            self.recent_trades.append(now)

            slip_pct = slippage_applied * 100
            print(
                f"  [LiveTrader] OPENED {direction} {coin} {timeframe}  "
                f"detected: {detected_price:.4f}  fill: {actual_fill_price:.4f}  "
                f"slip: {slip_pct:+.2f}%"
            )

            # 17. Return opened position
            return position

        finally:
            self._opening_position = False

    async def check_and_close(
        self,
        current_pm_up_price: float,
        btc_price: float | None = None,
        state=None,
    ) -> bool:
        """Evaluate TP/trailing/SL/full resolution and execute close order if triggered.

        All comparisons use tp_target / sl_target calculated on entry_price_real.
        pnl_usd is calculated as usdc_received - capital (exact, handles partial sells).
        """
        position = self.current_open_position
        if position is None or current_pm_up_price is None:
            return False

        # Time-based guard — wait at least 5s after open before evaluating TP/SL
        position["ticks_open"] = position.get("ticks_open", 0) + 1
        ts_open      = datetime.fromisoformat(position["timestamp_open"])
        seconds_open = (datetime.now(timezone.utc) - ts_open).total_seconds()
        if seconds_open < 5:
            return False

        direction   = position["direction"]
        entry_price = position["entry_price_real"]
        contracts   = position["contracts"]
        capital     = position["capital"]

        # Price of held contract
        current = (
            current_pm_up_price if direction == "LONG"
            else 1.0 - current_pm_up_price
        )

        # Update highest price
        if current > position.get("highest_price", 0):
            position["highest_price"] = current
        highest = position["highest_price"]

        tp = position["tp_target"]
        sl = position["sl_target"]

        new_status          = None
        exit_contract_price = None

        # 1. Take-profit
        if current >= tp:
            new_status          = "WIN_TP"
            exit_contract_price = current

        # 2. Trailing stop — armed at 0.72, fires on 12% drop from peak
        elif highest >= 0.72 and current <= round(highest - 0.12, 4):
            exit_contract_price = current
            new_status = "WIN_TRAIL" if exit_contract_price > entry_price else "LOSS_SL"

        # 3. Stop-loss
        elif current <= sl:
            new_status          = "LOSS_SL"
            exit_contract_price = current

        # 4. Full win resolution
        elif current >= 0.95:
            new_status          = "WIN_FULL"
            exit_contract_price = 1.00

        # 5. Full loss resolution
        elif current <= 0.05:
            new_status          = "LOSS_FULL"
            exit_contract_price = 0.00

        # Safety: WIN_TRAIL must never produce pnl <= 0
        if new_status == "WIN_TRAIL":
            if (contracts * exit_contract_price) - capital <= 0:
                new_status = "LOSS_SL"

        if new_status is None:
            # Persist highest_price update (no full recalc needed)
            for i, p in enumerate(self._data["positions"]):
                if p["id"] == position["id"]:
                    self._data["positions"][i] = position
                    break
            return False

        # ── Execute close via Polymarket ──────────────────────────────────────
        token_id    = position["token_id"]
        sell_amounts = [
            round(contracts * 0.99, 4),  # attempt 1
            round(contracts * 0.97, 4),  # attempt 2
            round(contracts * 0.95, 4),  # attempt 3
            round(contracts * 0.90, 4),  # attempt 4
        ]

        response = None
        for attempt, amount in enumerate(sell_amounts, 1):
            # Re-read freshest price if state is available
            if state is not None and state.pm_up is not None:
                latest_pm_up = state.pm_up
            else:
                latest_pm_up = current_pm_up_price
            latest_price = latest_pm_up if direction == "LONG" else 1.0 - latest_pm_up

            if attempt > 1:
                price_is_urgent = latest_price <= sl or latest_price <= (entry_price - 0.05)
                if price_is_urgent:
                    print(
                        f"[LiveTrader] ⚡ precio urgente ({latest_price:.4f} <= SL {sl:.4f}) "
                        f"— venta inmediata intento {attempt}"
                    )
                else:
                    delay = attempt - 1  # 1s, 2s, 3s for attempts 2/3/4
                    print(
                        f"[LiveTrader] ⏳ reintento {attempt} en {delay}s "
                        f"({amount:.4f} contratos) precio={latest_price:.4f}..."
                    )
                    await asyncio.sleep(delay)

            response = await self._try_sell_once(token_id, amount)
            if response is not None:
                print(f"[LiveTrader] ✅ venta ejecutada en intento {attempt}")
                break
            print(f"[LiveTrader] ⚠️ intento {attempt} sin liquidez")

        if response is None:
            position["status"]          = "CLOSE_FAILED"
            position["timestamp_close"] = datetime.now(timezone.utc).isoformat()
            # Activate cooldown — capital may still be committed on Polymarket
            self.last_close_timestamp = datetime.now(timezone.utc)
            print("⚠️  CLOSE_FAILED — cooldown activado, revisar posición manualmente")
        else:
            try:
                contracts_sold  = float(response["makingAmount"])
                usdc_received   = float(response["takingAmount"])
                exit_price_real = usdc_received / contracts_sold
                print(
                    f"[LiveTrader DEBUG] exit fill price: {exit_price_real:.4f} "
                    f"(taking={response['takingAmount']} making={response['makingAmount']})"
                )
                pnl = usdc_received - capital
            except (KeyError, ZeroDivisionError):
                contracts_sold  = contracts
                exit_price_real = exit_contract_price
                pnl             = (contracts_sold * exit_price_real) - capital

            # Reclassify based on real outcome (partial fills can flip the result)
            if pnl > 0 and new_status in ("LOSS_SL", "LOSS_FULL"):
                new_status = "WIN_TP"
            elif pnl <= 0 and new_status in ("WIN_TP", "WIN_TRAIL", "WIN_FULL"):
                new_status = "LOSS_SL"

            slip_pct = position.get("slippage_applied", 0) * 100

            _MSGS = {
                "WIN_TP":    f"✅ LIVE WIN TP    +${pnl:.2f} │ Fill: {entry_price:.2f}→{exit_price_real:.2f} │ Slip: {slip_pct:.2f}%",
                "WIN_TRAIL": f"✅ LIVE WIN TRAIL +${pnl:.2f} │ Fill: {entry_price:.2f}→{exit_price_real:.2f} │ Slip: {slip_pct:.2f}%",
                "WIN_FULL":  f"✅ LIVE WIN FULL  +${pnl:.2f} │ Fill: {entry_price:.2f}→{exit_price_real:.2f} │ Slip: {slip_pct:.2f}%",
                "LOSS_SL":   f"🛑 LIVE LOSS SL   -${abs(pnl):.2f} │ Fill: {entry_price:.2f}→{exit_price_real:.2f} │ Slip: {slip_pct:.2f}%",
                "LOSS_FULL": f"❌ LIVE LOSS FULL -${abs(pnl):.2f} │ Fill: {entry_price:.2f}→{exit_price_real:.2f} │ Slip: {slip_pct:.2f}%",
            }
            print(_MSGS.get(new_status, f"  [LiveTrader] closed {new_status} pnl=${pnl:.2f}"))

            position["status"]              = new_status
            position["contracts_sold"]      = contracts_sold
            position["exit_price_detected"] = exit_contract_price
            position["exit_price_real"]     = exit_price_real
            position["exit_btc_price"]      = btc_price
            position["timestamp_close"]     = datetime.now(timezone.utc).isoformat()
            position["pnl_usd"]             = pnl
            position["close_order_id"]      = response.get("id") or response.get("orderID") or ""

            self.daily_pnl            += pnl
            self.last_close_timestamp  = datetime.now(timezone.utc)

        # Update position in list
        closed_ok = position["status"] != "CLOSE_FAILED"
        for i, p in enumerate(self._data["positions"]):
            if p["id"] == position["id"]:
                self._data["positions"][i] = position
                break

        self._open_position = None
        self._recalc_summary()
        await asyncio.to_thread(self._save)
        return closed_ok

    def unrealized_pnl(self, current_pm_up_price: float) -> float | None:
        """Mark-to-market P&L for the open live position."""
        pos = self.current_open_position
        if pos is None or current_pm_up_price is None:
            return None
        direction = pos["direction"]
        current_contract = (
            current_pm_up_price if direction == "LONG"
            else 1.0 - current_pm_up_price
        )
        return (pos["contracts"] * current_contract) - pos["capital"]

    def _recent_volatility(self, state) -> float:
        v = calc_volatility(state.trades, state.mid or 1.0)
        return v if v >= 0 else 1.0  # treat no-data as volatile for entry guard

    def _log_error(self, method: str, error) -> None:
        """Append error to errors_log.json (JSONL). Never propagates."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "method":    method,
            "error":     str(error),
        }
        try:
            with open(_ERRORS_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass
