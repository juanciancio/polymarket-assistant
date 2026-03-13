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
from datetime import datetime, timezone
import time

from dotenv import load_dotenv

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
_COOLDOWN_SECS = 45
_BURST_WINDOW  = 300
_BURST_MAX     = 3

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

        # ── Persistent data ─────────────────────────────────────────────
        self._file = filepath
        self._data = self._load()
        self._data["summary"]["daily_loss_limit"] = self.daily_loss_limit

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
        for p in self._data["positions"]:
            if p["status"] == "OPEN":
                return p
        return None

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

    async def place_sell_order(self, token_id: str, contracts: float, current_price: float) -> dict | None:
        """Try up to 4 times with decreasing contract amounts and progressive delays."""
        attempts = [
            (round(contracts * 0.99, 4), 0),   # attempt 1 — 99%, immediate
            (round(contracts * 0.97, 4), 2),   # attempt 2 — 97%, wait 2s
            (round(contracts * 0.95, 4), 3),   # attempt 3 — 95%, wait 3s
            (round(contracts * 0.90, 4), 5),   # attempt 4 — 90%, wait 5s
        ]
        for attempt_num, (safe_contracts, delay) in enumerate(attempts, 1):
            if delay > 0:
                print(
                    f"[LiveTrader] ⏳ reintento {attempt_num} en {delay}s "
                    f"({safe_contracts:.4f} contratos)..."
                )
                await asyncio.sleep(delay)
            try:
                mo     = MarketOrderArgs(token_id=token_id, amount=safe_contracts, side=SELL)
                signed = await asyncio.to_thread(self.client.create_market_order, mo)
                resp   = await asyncio.to_thread(self.client.post_order, signed, OrderType.FOK)
                self.last_clob_call = time.time()
                if resp.get("status") != "canceled" and resp.get("success"):
                    print(f"[LiveTrader] ✅ venta ejecutada en intento {attempt_num}")
                    return resp
                print(f"[LiveTrader] ⚠️ intento {attempt_num} cancelado")
            except Exception as e:
                err = str(e)
                if "couldn't be fully filled" in err or "not enough balance" in err:
                    print(f"[LiveTrader] ⚠️ intento {attempt_num} sin liquidez")
                    continue
                self._log_error("place_sell_order", err)
                break

        self._log_error(
            "place_sell_order",
            f"CLOSE_FAILED después de {len(attempts)} intentos — "
            f"contratos={contracts:.4f} token={token_id[:16]}",
        )
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

        # 8. Price range validation
        if direction == "LONG" and not (0.55 <= detected_price <= 0.75):
            print(f"[LiveTrader] ❌ bloqueado: LONG precio fuera de rango ({detected_price:.4f})")
            return None
        if direction == "SHORT" and not (0.30 <= detected_price <= 0.70):
            print(f"[LiveTrader] ❌ bloqueado: SHORT precio fuera de rango ({detected_price:.4f})")
            return None

        # 9. Max price with slippage
        max_price = round(detected_price * (1 + self.slippage_tolerance), 4)
        print(f"[LiveTrader] direction={direction} detected={detected_price:.4f} max={max_price:.4f}")

        # 9b. Contract expiry guard — skip entry if < 90s remain
        if state.market_end_time is not None:
            remaining = (state.market_end_time - datetime.now(timezone.utc)).total_seconds()
            if remaining < 90:
                print(
                    f"[LiveTrader] ⏭️ contract expires in {remaining:.0f}s "
                    f"— skipping entry (minimum 90s required)"
                )
                return None

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
            needed    = self.max_position_size / detected_price
            if available < needed:
                print(
                    f"[LiveTrader] ⏭️ liquidez insuficiente — "
                    f"disponible={available:.2f} necesario={needed:.2f}"
                )
                return None
        except Exception as e:
            self._log_error("execute_signal.liquidity_check", str(e))
            return None

        # 12-13. Place FOK market buy — MarketOrderArgs handles sizing from USD amount
        response = await self.place_buy_order(token_id, self.max_position_size)
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

        # Guard: excessive real slippage — log but continue (position already open)
        real_slippage = (actual_fill_price - detected_price) / detected_price
        if abs(real_slippage) > 0.03:
            print(
                f"[LiveTrader] ⚠️ fill slippage too high: {real_slippage * 100:+.2f}% "
                f"(detected={detected_price:.4f} fill={actual_fill_price:.4f}) "
                f"— position opened in Polymarket but TP/SL may be unreliable"
            )
            self._log_error(
                "execute_signal",
                f"Excessive slippage: {real_slippage * 100:+.2f}% — "
                f"detected={detected_price:.4f} fill={actual_fill_price:.4f}",
            )

        print(f"[LiveTrader DEBUG] contracts reales del response: {contracts}")

        # Guard: contracts must not be absurdly large (protects against near-zero prices)
        _max_safe = self.max_position_size / 0.10
        if contracts > _max_safe:
            self._log_error(
                "execute_signal",
                f"Contracts absurdos: {contracts:.2f} (max={_max_safe:.2f}) — abortando",
            )
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
            "capital":              self.max_position_size,
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

    async def check_and_close(
        self,
        current_pm_up_price: float,
        btc_price: float | None = None,
    ) -> bool:
        """Evaluate TP/trailing/SL/full resolution and execute close order if triggered.

        All comparisons use tp_target / sl_target calculated on entry_price_real.
        pnl_usd is calculated on contracts (from entry_price_real) and exit_price_real.
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

        # 2. Trailing stop — armed at 0.75, fires on 12% drop from peak
        elif highest >= 0.75 and current <= round(highest - 0.12, 4):
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
        token_id = position["token_id"]

        response = await self.place_sell_order(token_id, contracts, current)

        if response is None:
            position["status"]          = "CLOSE_FAILED"
            position["timestamp_close"] = datetime.now(timezone.utc).isoformat()
            # Activate cooldown — capital may still be committed on Polymarket
            self.last_close_timestamp = datetime.now(timezone.utc)
            print("⚠️  CLOSE_FAILED — cooldown activado, revisar posición manualmente")
        else:
            exit_price_real = float(response.get("price", exit_contract_price))
            pnl             = (contracts * exit_price_real) - capital
            slip_pct        = position.get("slippage_applied", 0) * 100

            _MSGS = {
                "WIN_TP":    f"✅ LIVE WIN TP    +${pnl:.2f} │ Fill: {entry_price:.2f}→{exit_price_real:.2f} │ Slip: {slip_pct:.2f}%",
                "WIN_TRAIL": f"✅ LIVE WIN TRAIL +${pnl:.2f} │ Fill: {entry_price:.2f}→{exit_price_real:.2f} │ Slip: {slip_pct:.2f}%",
                "WIN_FULL":  f"✅ LIVE WIN FULL  +${pnl:.2f} │ Fill: {entry_price:.2f}→{exit_price_real:.2f} │ Slip: {slip_pct:.2f}%",
                "LOSS_SL":   f"🛑 LIVE LOSS SL   -${abs(pnl):.2f} │ Fill: {entry_price:.2f}→{exit_price_real:.2f} │ Slip: {slip_pct:.2f}%",
                "LOSS_FULL": f"❌ LIVE LOSS FULL -${abs(pnl):.2f} │ Fill: {entry_price:.2f}→{exit_price_real:.2f} │ Slip: {slip_pct:.2f}%",
            }
            print(_MSGS.get(new_status, f"  [LiveTrader] closed {new_status} pnl=${pnl:.2f}"))

            position["status"]              = new_status
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
