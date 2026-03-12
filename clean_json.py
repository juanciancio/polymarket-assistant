"""One-shot script: deduplicate paper_trades.json by position ID."""
import json

CLOSED = {"WIN_TP", "WIN_FULL", "WIN_TRAIL", "LOSS_SL", "LOSS_FULL"}
WIN    = {"WIN_TP", "WIN_FULL", "WIN_TRAIL"}

with open("paper_trades.json", encoding="utf-8") as f:
    data = json.load(f)

original_count = len(data["positions"])

# Keep only the first occurrence of each ID
seen = set()
clean = []
for p in data["positions"]:
    pid = p["id"]
    if pid not in seen:
        seen.add(pid)
        clean.append(p)

removed = original_count - len(clean)
data["positions"] = clean

# Recalculate summary
closed    = [p for p in clean if p["status"] in CLOSED]
n_wins    = sum(1 for p in closed if p["status"] in WIN)
n_closed  = len(closed)
total_pnl = sum(p["pnl"] for p in closed if p["pnl"] is not None)

data["summary"].update({
    "total_trades":      n_closed,
    "wins":              n_wins,
    "losses":            n_closed - n_wins,
    "open":              sum(1 for p in clean if p["status"] == "OPEN"),
    "win_rate":          (n_wins / n_closed * 100) if n_closed else 0.0,
    "total_pnl":         total_pnl,
    "avg_pnl_per_trade": (total_pnl / n_closed) if n_closed else 0.0,
    "capital_actual":    total_pnl,
})

with open("paper_trades.json", "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)

print(f"Duplicados eliminados: {removed}")
print(f"  total_trades={data['summary']['total_trades']}  "
      f"wins={data['summary']['wins']}  losses={data['summary']['losses']}  "
      f"win_rate={data['summary']['win_rate']:.2f}%  pnl=${data['summary']['total_pnl']:.4f}")
