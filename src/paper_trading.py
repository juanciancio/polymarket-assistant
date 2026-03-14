import json
import os
from datetime import datetime, timezone

CAPITAL = 10.0

TP_OFFSET    = 0.10   # take-profit: +10% over entry_pm_price
SL_OFFSET    = 0.15   # stop-loss:   -15% below entry_pm_price
TRAIL_DROP   = 0.12   # trailing stop: close if price drops 12% from highest
TRAIL_ARMED  = 0.72   # trailing stop only arms once highest_price >= this level

COOLDOWN_SECS  = 45   # minimum seconds between trades
BURST_WINDOW   = 300  # seconds for burst-limit window
BURST_MAX      = 3    # max trades allowed within BURST_WINDOW

WIN_STATUSES    = {"WIN_TP", "WIN_FULL", "WIN_TRAIL"}
LOSS_STATUSES   = {"LOSS_SL", "LOSS_FULL"}
CLOSED_STATUSES = WIN_STATUSES | LOSS_STATUSES

_EMPTY_SUMMARY = {
    "total_trades":      0,
    "wins":              0,
    "losses":            0,
    "open":              0,
    "win_rate":          0.0,
    "total_pnl":         0.0,
    "avg_pnl_per_trade": 0.0,
    "capital_inicial":   0,
    "capital_actual":    0.0,
}


class PaperTrader:
    def __init__(self, filepath: str):
        self._file = filepath
        self._data = self._load()

        # Session-only state (not persisted)
        self.last_close_timestamp: datetime | None = None
        self.recent_trades: list[datetime] = []

        # O(1) open-position cache — updated only on open/close
        self._open_position: dict | None = next(
            (p for p in self._data["positions"] if p["status"] == "OPEN"), None
        )

    # ── Persistence ─────────────────────────────────────────────

    def _load(self) -> dict:
        if os.path.exists(self._file):
            try:
                with open(self._file, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"summary": dict(_EMPTY_SUMMARY), "positions": []}

    def _save(self):
        with open(self._file, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)

    def _recalc_summary(self):
        positions = self._data["positions"]
        closed    = [p for p in positions if p["status"] in CLOSED_STATUSES]
        opens     = [p for p in positions if p["status"] == "OPEN"]
        n_wins    = sum(1 for p in closed if p["status"] in WIN_STATUSES)
        n_closed  = len(closed)
        total_pnl = sum(p["pnl"] for p in closed if p["pnl"] is not None)

        self._data["summary"].update({
            "total_trades":      n_closed,
            "wins":              n_wins,
            "losses":            n_closed - n_wins,
            "open":              len(opens),
            "win_rate":          (n_wins / n_closed * 100) if n_closed else 0.0,
            "total_pnl":         total_pnl,
            "avg_pnl_per_trade": (total_pnl / n_closed) if n_closed else 0.0,
            "capital_actual":    total_pnl,
        })

    # ── Public interface ─────────────────────────────────────────

    @property
    def summary(self) -> dict:
        return self._data["summary"]

    @property
    def current_open_position(self) -> dict | None:
        """Return the single OPEN position, or None. O(1) — cache updated on open/close."""
        return self._open_position

    @property
    def cooldown_remaining(self) -> int:
        """Seconds remaining in post-trade cooldown (0 if not active)."""
        if self.last_close_timestamp is None:
            return 0
        elapsed = (datetime.now(timezone.utc) - self.last_close_timestamp).total_seconds()
        return max(0, int(COOLDOWN_SECS - elapsed))

    @property
    def is_burst_limited(self) -> bool:
        """True if the burst-limit (BURST_MAX trades in BURST_WINDOW) is active."""
        now    = datetime.now(timezone.utc)
        recent = [t for t in self.recent_trades
                  if (now - t).total_seconds() < BURST_WINDOW]
        return len(recent) >= BURST_MAX

    def open_position(self, signal: dict, pm_up_price: float, btc_price: float,
                      coin: str, tf: str) -> dict | None:
        """Simulate opening a $10 position on MAX_CONVICTION signal.

        Guards (checked in order, all return None without writing to JSON):
            1. Price-range:  LONG 0.40–0.80, SHORT 0.20–0.60
            2. Cooldown:     COOLDOWN_SECS since last close
            3. Burst limit:  ≤ BURST_MAX trades within BURST_WINDOW seconds
        """
        cl        = signal["conviction_level"]
        direction = "LONG" if cl == "MAX_BULLISH" else "SHORT"

        if cl == "MAX_BULLISH":
            direction      = "LONG"
            detected_price = pm_up_price
        else:
            direction      = "SHORT"
            detected_price = 1.0 - pm_up_price


        # ── 1. Price-range guard ─────────────────────────────────
        if direction == "LONG" and not (0.55 <= detected_price <= 0.75):
            return None
        if direction == "SHORT" and not (0.30 <= detected_price <= 0.70):
            return None

        # ── 2. Cooldown guard ────────────────────────────────────
        remaining = self.cooldown_remaining
        if remaining > 0:
            print(f"⏳ Cooldown activo — esperando {remaining}s antes del próximo trade")
            return None

        # ── 3. Burst-limit guard ─────────────────────────────────
        now = datetime.now(timezone.utc)
        self.recent_trades = [t for t in self.recent_trades
                              if (now - t).total_seconds() < BURST_WINDOW]
        if len(self.recent_trades) >= BURST_MAX:
            print(f"⚠️  Límite de {BURST_MAX} trades por {BURST_WINDOW // 60} minutos alcanzado")
            return None

        # ── 4. LONG VWAP filter ──────────────────────────────────
        # Block LONG entries when price is below VWAP — all historical LONG
        # losses had this condition active.
        if direction == "LONG":
            triggered_conditions = signal.get("triggered_conditions", [])
            vwap_against = any(
                cond[0] == "Price below VWAP"
                for cond in triggered_conditions
            )
            if vwap_against:
                print("⏭️  LONG bloqueado — precio bajo VWAP")
                return None

        # ── 4b. SHORT VWAP filter ────────────────────────────────
        if direction == "SHORT":
            triggered_conditions = signal.get("triggered_conditions", [])
            vwap_against = any(
                cond[0] == "Price above VWAP"
                for cond in triggered_conditions
            )
            if vwap_against:
                print("⏭️  SHORT bloqueado — precio sobre VWAP")
                return None

        # ── Compute entry fields ─────────────────────────────────
        # LONG buys Up contract; SHORT buys Down contract (1 − pm_up_price)
        entry_pm_price = pm_up_price if direction == "LONG" else (1.0 - pm_up_price)

        # Extra guard: entry_pm_price must never be 0 or 1 (resolved contract)
        if entry_pm_price <= 0.0 or entry_pm_price >= 1.0:
            return None

        contracts  = CAPITAL / entry_pm_price
        tp_target  = round(entry_pm_price + TP_OFFSET, 4)
        sl_target  = round(entry_pm_price - SL_OFFSET, 4)

        pos_id   = len(self._data["positions"]) + 1
        position = {
            "id":                   pos_id,
            "timestamp_open":       now.isoformat(),
            "timestamp_close":      None,
            "coin":                 coin,
            "timeframe":            tf,
            "direction":            direction,
            "entry_pm_price":       entry_pm_price,
            "exit_pm_price":        None,
            "entry_btc_price":      btc_price,
            "exit_btc_price":       None,
            "capital":              CAPITAL,
            "contracts":            contracts,
            "tp_target":            tp_target,
            "sl_target":            sl_target,
            "highest_price":        entry_pm_price,
            "pnl":                  None,
            "status":               "OPEN",
            "ticks_open":           0,
            "score":                signal.get("score", 0),
            "conviction_level":     cl,
            "triggered_conditions": signal.get("triggered_conditions", []),
        }

        # Dedup guard — never write two records with the same ID
        existing_ids = {p["id"] for p in self._data["positions"]}
        if position["id"] in existing_ids:
            return None

        self._data["positions"].append(position)
        self._recalc_summary()
        self._open_position = position
        self.recent_trades.append(now)
        return position

    def check_resolution(self, position: dict, current_pm_up_price: float,
                         current_btc_price: float) -> dict:
        """Evaluate TP / trailing-stop / SL / full resolution for the open position.

        Contract-price semantics:
            LONG  → holds UP contract   → current_price = current_pm_up_price
            SHORT → holds DOWN contract → current_price = 1 − current_pm_up_price

        entry_pm_price is always the price of the held contract at open
        (UP price for LONG, DOWN price for SHORT — set in open_position()).

        highest_price tracks the peak of the held contract's price, enabling
        a symmetric trailing stop for both directions.

        Priority order:
            1. WIN_TP    — held contract price crossed tp_target
            2. WIN_TRAIL — trailing stop triggered AND current_price > entry_price (profit)
                           → LOSS_SL if current_price <= entry_price (loss despite stop)
            3. LOSS_SL   — held contract price fell below sl_target
            4. WIN_FULL  — contract resolved to $1.00 (price >= 0.95)
            5. LOSS_FULL — contract resolved to $0.00 (price <= 0.05)
        """
        # ── time-based guard — wait at least 5s after open before evaluating TP/SL
        position["ticks_open"] = position.get("ticks_open", 0) + 1
        ts_open      = datetime.fromisoformat(position["timestamp_open"])
        seconds_open = (datetime.now(timezone.utc) - ts_open).total_seconds()
        if seconds_open < 5:
            return position

        direction   = position["direction"]
        entry_price = position["entry_pm_price"]   # price of held contract at open
        contracts   = position["contracts"]
        capital     = position["capital"]

        # ── price of the contract we're holding ──────────────────
        if direction == "SHORT":
            current_price = 1.0 - current_pm_up_price  # DOWN contract
        else:
            current_price = current_pm_up_price         # UP contract

        # ── update highest_price (peak of held contract) ─────────
        if current_price > position.get("highest_price", 0):
            position["highest_price"] = current_price
        highest = position["highest_price"]

        tp  = position.get("tp_target", entry_price + TP_OFFSET)
        sl  = position.get("sl_target", entry_price - SL_OFFSET)

        new_status = None
        exit_price = None

        # 1. Take-profit
        if current_price >= tp:
            new_status = "WIN_TP"
            exit_price = current_price

        # 2. Trailing stop — armed once highest reached TRAIL_ARMED, fires on TRAIL_DROP
        elif highest >= TRAIL_ARMED and current_price <= round(highest - TRAIL_DROP, 4):
            exit_price = current_price
            # Only WIN if we exit above entry; otherwise classify as LOSS_SL
            if exit_price > entry_price:
                new_status = "WIN_TRAIL"
            else:
                new_status = "LOSS_SL"

        # 3. Stop-loss
        elif current_price <= sl:
            new_status = "LOSS_SL"
            exit_price = current_price

        # 4. Full win resolution
        elif current_price >= 0.95:
            new_status = "WIN_FULL"
            exit_price = 1.00

        # 5. Full loss resolution
        elif current_price <= 0.05:
            new_status = "LOSS_FULL"
            exit_price = 0.00

        # Safety: WIN_TRAIL must never produce pnl <= 0
        if new_status == "WIN_TRAIL":
            pnl_check = (contracts * exit_price) - capital
            if pnl_check <= 0:
                new_status = "LOSS_SL"

        if new_status is not None:
            position["status"]          = new_status
            position["exit_pm_price"]   = exit_price
            position["exit_btc_price"]  = current_btc_price
            position["timestamp_close"] = datetime.now(timezone.utc).isoformat()
            position["pnl"]             = self.calculate_pnl(position)

            for i, p in enumerate(self._data["positions"]):
                if p["id"] == position["id"]:
                    self._data["positions"][i] = position
                    break

            self._recalc_summary()
            self._open_position = None
            self.last_close_timestamp = datetime.now(timezone.utc)

        return position

    def calculate_pnl(self, position: dict) -> float | None:
        """Profit/loss in USD: (contracts × exit_pm_price) − capital."""
        exit_pm = position.get("exit_pm_price")
        if exit_pm is None:
            return None
        return (position["contracts"] * exit_pm) - position["capital"]

    def unrealized_pnl(self, current_pm_up_price: float) -> float | None:
        """Mark-to-market P&L for the open position, or None if no open position."""
        pos = self.current_open_position
        if pos is None or current_pm_up_price is None:
            return None
        direction = pos["direction"]
        current_contract = (
            current_pm_up_price if direction == "LONG" else (1.0 - current_pm_up_price)
        )
        return (pos["contracts"] * current_contract) - pos["capital"]
