import json
import os
from datetime import datetime, timezone

CAPITAL = 10.0

TP_OFFSET    = 0.10   # take-profit: +10% over entry_pm_price
SL_OFFSET    = 0.15   # stop-loss:   -15% below entry_pm_price
TRAIL_DROP   = 0.12   # trailing stop: close if price drops 12% from highest
TRAIL_ARMED  = 0.75   # trailing stop only arms once highest_price >= this level

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
        """Return the single OPEN position, or None."""
        for p in self._data["positions"]:
            if p["status"] == "OPEN":
                return p
        return None

    def open_position(self, signal: dict, pm_up_price: float, btc_price: float,
                      coin: str, tf: str) -> dict | None:
        """Simulate opening a $10 position on MAX_CONVICTION signal.

        Args:
            signal:       dict with keys conviction_level, score, triggered_conditions.
            pm_up_price:  current best-ask of the PM Up contract (0-1).
            btc_price:    current Binance mid price (used as entry_btc_price label).
            coin:         e.g. "BTC".
            tf:           e.g. "15m".

        Returns:
            The newly created position dict, or None if pm_up_price is out of range.

        Valid entry ranges:
            LONG  → 0.40 <= pm_up_price <= 0.80  (Up contract has meaningful upside)
            SHORT → 0.20 <= pm_up_price <= 0.60  (Down contract = 1-pm_up has upside)
        """
        cl        = signal["conviction_level"]
        direction = "LONG" if cl == "MAX_BULLISH" else "SHORT"

        # Price-range guard — reject entries with poor risk/reward or near-resolved contracts
        if direction == "LONG" and not (0.40 <= pm_up_price <= 0.80):
            return None
        if direction == "SHORT" and not (0.20 <= pm_up_price <= 0.60):
            return None

        # LONG buys Up contract; SHORT buys Down contract (1 - pm_up_price)
        entry_pm_price = pm_up_price if direction == "LONG" else (1.0 - pm_up_price)
        contracts      = CAPITAL / entry_pm_price
        tp_target      = round(entry_pm_price + TP_OFFSET, 4)
        sl_target      = round(entry_pm_price - SL_OFFSET, 4)

        pos_id   = len(self._data["positions"]) + 1
        position = {
            "id":                   pos_id,
            "timestamp_open":       datetime.now(timezone.utc).isoformat(),
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

        self._data["positions"].append(position)
        self._recalc_summary()
        self._save()
        return position

    def check_resolution(self, position: dict, current_pm_up_price: float,
                         current_btc_price: float) -> dict:
        """Evaluate TP / trailing-stop / SL / full resolution for the open position.

        Priority order (both directions):
            1. WIN_TP    — contract price crossed tp_target
            2. WIN_TRAIL — trailing stop triggered (armed at TRAIL_ARMED, drops TRAIL_DROP)
            3. LOSS_SL   — contract price fell below sl_target
            4. WIN_FULL  — contract resolved to $1.00 (price >= 0.95)
            5. LOSS_FULL — contract resolved to $0.00 (price <= 0.05)

        For SHORT, all comparisons use down_price = 1 − pm_up_price.

        Returns the position dict (mutated in-place if resolved, otherwise with updated
        highest_price only).
        """
        # ── tick guard ───────────────────────────────────────────
        position["ticks_open"] = position.get("ticks_open", 0) + 1
        if position["ticks_open"] < 3:
            return position

        direction = position["direction"]

        # ── contract price from our perspective ──────────────────
        cur = current_pm_up_price if direction == "LONG" else (1.0 - current_pm_up_price)

        # ── update highest_price (trailing stop tracking) ────────
        highest = max(position.get("highest_price", cur), cur)
        position["highest_price"] = highest

        tp  = position.get("tp_target", cur + TP_OFFSET)
        sl  = position.get("sl_target", cur - SL_OFFSET)
        trail_stop = round(highest - TRAIL_DROP, 4)

        new_status = None
        exit_price = None

        # 1. Take-profit
        if cur >= tp:
            new_status = "WIN_TP"
            exit_price = cur

        # 2. Trailing stop (armed once highest reached TRAIL_ARMED)
        elif highest >= TRAIL_ARMED and cur <= trail_stop:
            new_status = "WIN_TRAIL"
            exit_price = cur

        # 3. Stop-loss
        elif cur <= sl:
            new_status = "LOSS_SL"
            exit_price = cur

        # 4. Full win resolution
        elif cur >= 0.95:
            new_status = "WIN_FULL"
            exit_price = 1.00

        # 5. Full loss resolution
        elif cur <= 0.05:
            new_status = "LOSS_FULL"
            exit_price = 0.00

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
            self._save()

        return position

    def calculate_pnl(self, position: dict) -> float | None:
        """Profit/loss in USD: (contracts × exit_pm_price) − capital.

        Works for all exit types:
          WIN_TP / WIN_TRAIL → exit at real market price (partial gain)
          WIN_FULL           → exit at 1.00
          LOSS_SL            → exit at real market price (partial loss)
          LOSS_FULL          → exit at 0.00 → always −$10.00
        """
        exit_pm = position.get("exit_pm_price")
        if exit_pm is None:
            return None

        pnl = (position["contracts"] * exit_pm) - position["capital"]

        if position.get("status") in WIN_STATUSES and pnl == 0.0:
            print(
                f"[paper_trading] WARNING: {position.get('status')} trade "
                f"#{position.get('id')} produced pnl=0.00 — "
                f"contracts={position['contracts']}, entry_pm={position['entry_pm_price']}"
            )

        return pnl

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
