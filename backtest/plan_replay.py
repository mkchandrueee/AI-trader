"""
Replay a stop/exit plan against recorded NSE bars.

Shared by .claude/skills/trader-timemachine and backend/skills_api.py.

Not a prediction. It takes a plan -- entry, stop, exit -- and replays what would
have happened on each recent cached session, bar by bar, with a same-bar tie
going to the STOP. That tie rule is deliberately pessimistic: when one bar's
range spans both levels there is no way to know which came first, and assuming
the good one is how backtests lie.
"""
from __future__ import annotations

MAX_BARS = 24          # about two hours of 5-minute bars, the agent's own hold cap


def replay_plan(symbol: str, entry: float, stop: float, exit_px: float,
                sessions: int = 12) -> dict:
    import pandas as pd
    from config import measured_costs
    from data.intraday_bars_store import load_bars

    df = load_bars(symbol)
    if df is None or df.empty:
        return {"error": f"No cached bars for {symbol}. Sync the Intraday Engine first."}

    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df["day"] = df["timestamp"].dt.date
    days = sorted(df["day"].unique())[-sessions:]
    long = exit_px > entry
    # Equities: ASSUMED, because this platform collects no equity quotes to
    # measure a spread from. Reported alongside the result rather than buried.
    cost_frac = measured_costs.ASSUMED_EQUITY_ROUND_TRIP

    outcomes = []
    for d in days:
        bars = df[df["day"] == d].reset_index(drop=True)
        start = next((i for i in range(len(bars)) if bars.low[i] <= entry <= bars.high[i]), None)
        if start is None:
            outcomes.append({"day": str(d), "result": "no fill"})
            continue
        result = "open at cap"
        px = float(bars.close.iloc[min(start + MAX_BARS, len(bars) - 1)])
        for i in range(start, min(start + MAX_BARS, len(bars))):
            hi, lo = float(bars.high[i]), float(bars.low[i])
            hit_stop = lo <= stop if long else hi >= stop
            hit_exit = hi >= exit_px if long else lo <= exit_px
            if hit_stop:                      # same-bar tie -> stop wins
                result, px = "stop", stop
                break
            if hit_exit:
                result, px = "exit", exit_px
                break
        gross = (px - entry) / entry * (1 if long else -1)
        outcomes.append({"day": str(d), "result": result, "fill": round(px, 2),
                         "net_pct": round(100 * (gross - cost_frac), 3)})

    traded = [o for o in outcomes if o["result"] != "no fill"]
    nets = [o["net_pct"] for o in traded]
    return {
        "symbol": symbol, "entry": entry, "stop": stop, "exit": exit_px,
        "direction": "long" if long else "short",
        "sessions_examined": len(days), "filled": len(traded),
        "cost_pct_applied": round(100 * cost_frac, 3),
        "cost_source": ("ASSUMED for equities -- no equity quotes are collected, "
                        "so this figure is not measured"),
        "wins": sum(1 for o in traded if o["result"] == "exit"),
        "stops": sum(1 for o in traded if o["result"] == "stop"),
        "timeouts": sum(1 for o in traded if o["result"] == "open at cap"),
        "mean_net_pct": round(sum(nets) / len(nets), 3) if nets else None,
        "outcomes": outcomes,
    }
