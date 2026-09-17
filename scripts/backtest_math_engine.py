"""
Historical backtest harness for strategy/math_decision_strategy.py's
analyse_option_pair() -- replays the exact entry/exit rules
strategy/intraday_agent.py uses live (opening/latest 5-min candle staging,
one position per symbol at a time, flat exit-the-whole-lot at partial or
target or stop or EOD) against real historical NIFTY ATM CE/PE
minute_candles (tick_data when available, via
backtest.option_resolver.load_option_premiums_for_day -- same data source
scripts/tick_replay_backtest.py already trusts).

Exists to answer two questions the live paper-trade sample (67 trades
after the double-exit-race fix, profit_factor 0.56) is too small and too
slow to answer safely:
  1. Would exiting the whole lot at `target` (entry+20 points) instead of
     `partial` (entry+10) have performed differently? (paper trades only
     ever recorded sparse ~3-point journeys, too coarse to replay this.)
  2. Is the MIN_RR>=1.0 gate (strategy/intraday_agent.py, added after
     finding it would have cut the 67-trade sample's loss 89%) still a
     good idea across more history than the 67 trades it was sized from?

NIFTY only -- backtest.option_resolver.get_nearest_expiry() and the DB
option-symbol alias format it reads are NIFTY-specific (see that module's
own docstring). BANKNIFTY/SENSEX would need the live
AngelOne-instrument-master resolution path strategy/premarket.py uses,
which has no historical-replay equivalent.

Entry/exit logic is a deliberately faithful port of
strategy/intraday_agent.py's run_cycle()/check_exits(), not a
reimplementation -- same DEFAULT_CFG, same MIN_RR constant (imported
directly, not hand-copied, so this backtest can never silently drift from
the live agent's actual config), same one-per-symbol/opening-then-latest
staging, same EOD_SQUAREOFF time.

Usage:
    python scripts/backtest_math_engine.py                       # all available days, current live config (exit=partial, MIN_RR gate on)
    python scripts/backtest_math_engine.py 2026-09-08 2026-09-10  # specific dates
    python scripts/backtest_math_engine.py --exit-field target
    python scripts/backtest_math_engine.py --min-rr 0             # disable the gate (pre-fix behaviour)
    python scripts/backtest_math_engine.py --compare              # all 4 combinations side by side
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

from database.db import read_sql
from backtest.option_resolver import (
    get_nearest_expiry, build_option_symbol, get_atm_strike,
    load_option_premiums_for_day,
)
from strategy.math_decision_strategy import analyse_option_pair, DEFAULT_CFG
from strategy.intraday_agent import MIN_RR as LIVE_MIN_RR, EOD_SQUAREOFF as LIVE_EOD_SQUAREOFF
from utils.logger import get_logger

logger = get_logger("backtest_math_engine")

LOT_SIZE = 65
COMMISSION = 40.0  # round-trip, matches scripts/tick_replay_backtest.py's COMMISSION
STRIKE_GAP = 50
SESSION_START = dtime(9, 15)
OPENING_CLOSE = dtime(9, 20)      # end of the fixed opening 5-min candle
LAST_ENTRY_BAR = dtime(15, 20)    # last 5-min bar boundary allowed to open a NEW position
EOD_SQUAREOFF = LIVE_EOD_SQUAREOFF  # 15:25 -- imported, not hand-copied


@dataclass
class BTTrade:
    date: str
    entry_time: datetime
    mode: str
    side: str
    option_symbol: str
    strike: int
    entry: float
    stop: float
    partial: float
    target: float
    rr: float
    confidence: int
    tier: str
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    pnl: Optional[float] = None


def get_available_days(explicit_dates: Optional[list[str]] = None) -> list[date]:
    if explicit_dates:
        return sorted(datetime.strptime(d, "%Y-%m-%d").date() for d in explicit_dates)
    df = read_sql(
        "SELECT DISTINCT timestamp::date AS d FROM minute_candles "
        "WHERE symbol ~ '^NIFTY[0-9]{6}[0-9]+(CE|PE)$' ORDER BY 1",
        {},
    )
    return sorted(df["d"].tolist())


def _floor_5min(ts: pd.Timestamp) -> pd.Timestamp:
    return ts - timedelta(minutes=ts.minute % 5, seconds=ts.second, microseconds=ts.microsecond)


def _resample_5min(df: pd.DataFrame) -> pd.DataFrame:
    """1-min OHLC rows -> 5-min OHLC, bucketed by bar START time. A bucket
    labelled 09:15 covers [09:15, 09:20) -- i.e. the candle that CLOSES at
    09:20, matching OPENING_WINDOW's own ("09:15","09:20") convention."""
    if df.empty:
        return df
    d = df.copy()
    d["bucket"] = d["timestamp"].apply(_floor_5min)
    out = d.groupby("bucket").agg(
        open=("open", "first"), high=("high", "max"),
        low=("low", "min"), close=("close", "last"),
    ).reset_index()
    return out


def _to_ist_naive(ts: pd.Series) -> pd.Series:
    """minute_candles.timestamp is genuine tz-aware UTC (confirmed directly
    against the data: the first NIFTY-I tick of a session lands at 03:44
    UTC == 09:14 IST, one minute before market open) -- convert to IST and
    drop tzinfo so it compares naturally against the naive dtime
    constants (SESSION_START etc) the rest of this codebase uses for
    wall-clock IST comparisons."""
    return ts.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)


def _load_1min(symbol: str, trading_date: date) -> pd.DataFrame:
    df = read_sql(
        "SELECT timestamp, open, high, low, close FROM minute_candles "
        "WHERE symbol = :sym AND timestamp::date = :dt ORDER BY timestamp",
        {"sym": symbol, "dt": str(trading_date)},
    )
    if not df.empty:
        df["timestamp"] = _to_ist_naive(pd.to_datetime(df["timestamp"], utc=True))
    return df


def _simulate_exit(opt_symbol: str, trading_date: date, entry_dt: datetime,
                    stop: float, exit_target: float) -> tuple[datetime, float, str]:
    """
    Walk the option's recorded premium series forward from entry_dt,
    tick-by-tick if tick_data exists for the day (>=50 ticks), else
    1-min-bar-by-bar -- exactly the dual-resolution source
    scripts/tick_replay_backtest.py already relies on
    (option_resolver.load_option_premiums_for_day). Same-bar ties (a
    single 1-min bar's range spans both stop and target) resolve to STOP
    first -- conservative, avoids look-ahead optimism; tick mode has true
    chronological order so this only matters in candle-mode bars.
    """
    prem_df = load_option_premiums_for_day(opt_symbol, trading_date)
    eod_dt = datetime.combine(trading_date, EOD_SQUAREOFF)
    if prem_df.empty:
        return eod_dt, stop, "NO_DATA"  # can't observe the trade at all -- flagged, not silently dropped

    mode = prem_df.attrs.get("_mode", "candle")
    # option_resolver.load_option_premiums_for_day() returns its own
    # tz-aware-UTC timestamp column (same TIMESTAMPTZ source as
    # minute_candles) -- convert to IST-naive here so it compares
    # correctly against entry_dt, which came from the already-converted
    # index bars. Cached by option_resolver itself, so converting a copy
    # here doesn't mutate what other callers (e.g.
    # scripts/tick_replay_backtest.py) see from the shared cache.
    prem_df = prem_df.copy()
    ts = prem_df["timestamp"]
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize("UTC")
    prem_df["timestamp"] = _to_ist_naive(ts)
    window = prem_df[prem_df["timestamp"] >= entry_dt]
    last_price = None

    if mode == "tick":
        for _, row in window.iterrows():
            ts, px = row["timestamp"], row["premium"]
            last_price = px
            if ts.time() >= EOD_SQUAREOFF:
                return ts, px, "EOD"
            if px <= stop:
                return ts, px, "STOP"
            if px >= exit_target:
                return ts, px, "TARGET"
    else:
        for _, row in window.iterrows():
            ts = row["timestamp"]
            if ts.time() >= EOD_SQUAREOFF:
                return ts, (last_price if last_price is not None else row["premium"]), "EOD"
            lo, hi, close = row["low"], row["high"], row["premium"]
            last_price = close
            if lo <= stop:
                return ts, stop, "STOP"
            if hi >= exit_target:
                return ts, exit_target, "TARGET"

    # Ran out of data before EOD (day's option feed stopped early) -- close
    # at the last observed price rather than fabricate one.
    return (window["timestamp"].iloc[-1] if not window.empty else entry_dt), \
           (last_price if last_price is not None else stop), "DATA_END"


def replay_day(trading_date: date, exit_field: str, min_rr: float,
               cfg: dict) -> list[BTTrade]:
    expiry = get_nearest_expiry(trading_date)
    if expiry is None:
        logger.warning(f"{trading_date}: no expiry resolved, skipping")
        return []

    index_1min = _load_1min("NIFTY-I", trading_date)
    if index_1min.empty:
        return []
    index_5min = _resample_5min(index_1min)
    index_5min = index_5min[
        (index_5min["bucket"].dt.time >= SESSION_START) &
        (index_5min["bucket"].dt.time <= LAST_ENTRY_BAR)
    ].reset_index(drop=True)
    if index_5min.empty:
        return []

    # Every ATM strike touched this session, bulk-resolved once. (Only
    # 5-min bars are needed here for signal generation -- exit simulation
    # loads its own finer-grained series per trade, on demand, via
    # load_option_premiums_for_day()'s own tick-or-candle cache.)
    strikes = sorted({get_atm_strike(px, STRIKE_GAP) for px in index_5min["close"]})
    option_5min: dict[tuple[int, str], pd.DataFrame] = {}
    for strike in strikes:
        for opt_type in ("CE", "PE"):
            sym = build_option_symbol(expiry, strike, opt_type)
            option_5min[(strike, opt_type)] = _resample_5min(_load_1min(sym, trading_date))

    trades: list[BTTrade] = []
    # The whole point of "one position per symbol at a time" is that NO
    # bar between entry and exit is eligible for a new signal -- exit is
    # resolved synchronously (we already know the full outcome the moment
    # a trade opens), so track the wall-clock time it actually closes and
    # skip every bar up to it, not just the single bar it opened on.
    blocked_until: Optional[datetime] = None

    for _, bar in index_5min.iterrows():
        bar_end = bar["bucket"] + timedelta(minutes=5)

        if blocked_until is not None and bar_end <= blocked_until:
            continue

        spot = bar["close"]
        strike = get_atm_strike(spot, STRIKE_GAP)
        ce_df = option_5min.get((strike, "CE"), pd.DataFrame())
        pe_df = option_5min.get((strike, "PE"), pd.DataFrame())
        ce_row = ce_df[ce_df["bucket"] == bar["bucket"]] if not ce_df.empty else ce_df
        pe_row = pe_df[pe_df["bucket"] == bar["bucket"]] if not pe_df.empty else pe_df
        if ce_row.empty or pe_row.empty:
            continue  # mirrors live decision.get("error") -- candle data missing this bar

        call_ohlc = tuple(ce_row.iloc[0][["open", "high", "low", "close"]])
        put_ohlc = tuple(pe_row.iloc[0][["open", "high", "low", "close"]])

        is_opening_bar = bar["bucket"].time() == SESSION_START
        # Faithful-enough simplification of run_cycle()'s opening_fired
        # bookkeeping: the live agent re-tries "opening" every cycle until
        # it fires, but re-evaluating the SAME fixed candle never changes
        # the decision, so trying it once is equivalent -- just skip the
        # (redundant) repeat evaluation on later bars.
        mode = "opening" if is_opening_bar else "latest"

        decision = analyse_option_pair(call_ohlc, put_ohlc, cfg)
        if not decision.tradable:
            continue
        if min_rr > 0 and decision.rr == decision.rr and decision.rr < min_rr:  # NaN-safe
            continue

        side = decision.side
        opt_type = "CE" if side == "call" else "PE"
        opt_symbol = build_option_symbol(expiry, strike, opt_type)
        entry_dt = bar_end  # decision is made on the candle that just closed
        exit_target = decision.partial if exit_field == "partial" else decision.target

        open_trade = BTTrade(
            date=str(trading_date), entry_time=entry_dt, mode=mode, side=side,
            option_symbol=opt_symbol, strike=strike, entry=decision.entry,
            stop=decision.stop, partial=decision.partial, target=decision.target,
            rr=decision.rr, confidence=decision.confidence, tier=decision.tier,
        )
        exit_dt, exit_px, reason = _simulate_exit(opt_symbol, trading_date, entry_dt,
                                                    decision.stop, exit_target)
        open_trade.exit_time = exit_dt
        open_trade.exit_price = exit_px
        open_trade.exit_reason = reason
        open_trade.pnl = round((exit_px - decision.entry) * LOT_SIZE - COMMISSION, 2)
        trades.append(open_trade)
        blocked_until = exit_dt

    return trades


def run_backtest(dates: list[date], exit_field: str, min_rr: float,
                  cfg: dict = None) -> list[BTTrade]:
    cfg = cfg or DEFAULT_CFG
    all_trades: list[BTTrade] = []
    for d in dates:
        try:
            day_trades = replay_day(d, exit_field, min_rr, cfg)
            all_trades.extend(day_trades)
        except Exception as e:
            logger.error(f"{d}: replay failed: {e}")
    return all_trades


UNOBSERVABLE_REASONS = {"NO_DATA", "DATA_END"}


def summarize(label: str, trades: list[BTTrade]) -> dict:
    n = len(trades)
    # DATA_END means the option's historical minute_candles ran dry before
    # we observed a real stop/target/EOD cross -- illiquid-strike gaps in
    # what was actually collected live, not a trade outcome. Counting it
    # as a loss (its fallback exit price is pessimistic-by-construction:
    # last observed price, or the stop level itself if there was no data
    # at all after entry) silently manufactures a worse-than-real profit
    # factor. Excluded from every stat below; reported as its own count
    # instead so the user can judge how much of the sample is unobservable
    # rather than have it invisibly baked into the numbers.
    valid = [t for t in trades if t.exit_reason not in UNOBSERVABLE_REASONS]
    wins = [t for t in valid if (t.pnl or 0) > 0]
    losses = [t for t in valid if (t.pnl or 0) <= 0]
    total_pnl = sum(t.pnl or 0 for t in valid)
    gross_win = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    pf = gross_win / gross_loss if gross_loss else float("inf")
    win_rate = 100 * len(wins) / len(valid) if valid else 0
    unobservable = n - len(valid)
    print(f"\n{label}")
    print(f"  n={len(valid)}" + (f"  ({unobservable} more unobservable: data ran out before a real exit, excluded)" if unobservable else ""))
    print(f"  win_rate={win_rate:.1f}%  total_pnl=Rs.{total_pnl:,.0f}  profit_factor={pf:.2f}")
    if valid:
        print(f"  avg_pnl/trade=Rs.{total_pnl/len(valid):,.0f}")
    return {"label": label, "n": len(valid), "win_rate": win_rate, "total_pnl": total_pnl, "pf": pf, "unobservable": unobservable}


def main():
    parser = argparse.ArgumentParser(description="Historical backtest for math_decision_engine")
    parser.add_argument("dates", nargs="*", help="Dates to replay (YYYY-MM-DD). Default: all available.")
    parser.add_argument("--exit-field", choices=["partial", "target"], default="partial",
                         help="Which decision field to exit the whole lot at (default: partial, matches live).")
    parser.add_argument("--min-rr", type=float, default=LIVE_MIN_RR,
                         help=f"Minimum decision.rr to take a trade (default: {LIVE_MIN_RR}, the live MIN_RR). 0 disables the gate.")
    parser.add_argument("--compare", action="store_true",
                         help="Run all 4 combinations of exit-field x min-rr-gate side by side, ignoring --exit-field/--min-rr.")
    args = parser.parse_args()

    dates = get_available_days(args.dates or None)
    if not dates:
        print("No historical days with NIFTY option candle data found.")
        return
    print(f"Replaying {len(dates)} day(s): {dates[0]} -> {dates[-1]}")

    if args.compare:
        results = []
        for exit_field in ("partial", "target"):
            for gate in (0.0, LIVE_MIN_RR):
                trades = run_backtest(dates, exit_field, gate)
                gate_label = "gate OFF" if gate == 0 else f"gate>={gate}"
                results.append(summarize(f"exit={exit_field:<8} {gate_label}", trades))
        print("\n" + "=" * 70)
        print("SUMMARY")
        print("=" * 70)
        for r in results:
            print(f"  {r['label']:<28} n={r['n']:<4} win%={r['win_rate']:5.1f}  "
                  f"pnl=Rs.{r['total_pnl']:>9,.0f}  PF={r['pf']:.2f}  (unobservable={r['unobservable']})")
    else:
        trades = run_backtest(dates, args.exit_field, args.min_rr)
        gate_label = "OFF" if args.min_rr == 0 else f">={args.min_rr}"
        summarize(f"exit={args.exit_field}  MIN_RR gate {gate_label}", trades)


if __name__ == "__main__":
    main()
