"""
Historical study of the reference-app Options Analyzer score and Pullback
Entry (strategy/math_decision_strategy.py: analyzer_breakdown / pullback_entry)
on real NIFTY ATM CE/PE 1-minute candles -- read-only, DB only, no broker.

The analyzer's 0-100 score is a candle-shape checklist the reference app
publishes, not a fitted probability. This script asks whether it actually
carries edge on our data, and which entry style (breakout at the analyzer's
entry, or a limit buy in the pullback zones) trades it best.

Signal: every closed 5-min ATM pair whose verdict is YES (leader score >= the
threshold and gap >= 20). One position at a time, exit the whole lot, same
1-minute-candle walk as scripts/backtest_math_engine.py (same-bar tie -> STOP
first, EOD at the live agent's square-off time, commission Rs.40 round trip).

Variants:
  breakout-T1/T2  buy when price trades up to the analyzer entry (within 3
                  bars), SL = ladder stop, exit at ladder T1 / T2
  pb-zoneN        limit buy at pullback zone N (within 3 bars), SL = pullback
                  SL, exit at the pullback T1 (zone 2 + step)
Each is reported for leader-score cut-offs 60 and 75, plus the live engine's
own baseline over the same days.

Usage:
    python scripts/backtest_analyzer.py
    python scripts/backtest_analyzer.py 2026-09-08 2026-09-10
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

from backtest.option_resolver import get_nearest_expiry, build_option_symbol, get_atm_strike
from scripts.backtest_math_engine import (
    COMMISSION, EOD_SQUAREOFF, LAST_ENTRY_BAR, LIVE_MIN_RR, LOT_SIZE, SESSION_START, STRIKE_GAP,
    _load_1min, _resample_5min, get_available_days, run_backtest, summarize,
)
from strategy.math_decision_strategy import analyzer_breakdown, pullback_entry

FILL_WINDOW_BARS = 3  # 1-min bars are checked for FILL_WINDOW_BARS * 5 minutes after the signal


@dataclass
class Signal:
    day: date
    bar_end: datetime
    side: str
    symbol: str
    leader: int
    ladder: dict
    pullback: dict


@dataclass
class Trade:
    day: date
    variant: str
    leader: int
    pnl: float
    reason: str


def collect_signals(day: date) -> tuple[list[Signal], dict[str, pd.DataFrame]]:
    expiry = get_nearest_expiry(day)
    if expiry is None:
        return [], {}
    idx = _resample_5min(_load_1min("NIFTY-I", day))
    if idx.empty:
        return [], {}
    idx = idx[(idx["bucket"].dt.time >= SESSION_START) & (idx["bucket"].dt.time <= LAST_ENTRY_BAR)]

    series: dict[str, pd.DataFrame] = {}
    five: dict[str, pd.DataFrame] = {}

    def load(sym: str):
        if sym not in series:
            series[sym] = _load_1min(sym, day)
            five[sym] = _resample_5min(series[sym])
        return five[sym]

    signals: list[Signal] = []
    for _, bar in idx.iterrows():
        strike = get_atm_strike(bar["close"], STRIKE_GAP)
        ce_sym = build_option_symbol(expiry, strike, "CE")
        pe_sym = build_option_symbol(expiry, strike, "PE")
        ce, pe = load(ce_sym), load(pe_sym)
        if ce.empty or pe.empty:
            continue
        ce_row, pe_row = ce[ce["bucket"] == bar["bucket"]], pe[pe["bucket"] == bar["bucket"]]
        if ce_row.empty or pe_row.empty:
            continue
        ce_ohlc = tuple(ce_row.iloc[0][["open", "high", "low", "close"]])
        pe_ohlc = tuple(pe_row.iloc[0][["open", "high", "low", "close"]])
        a = analyzer_breakdown(ce_ohlc, pe_ohlc)
        v = a["verdict"]
        if v["side"] is None:
            continue
        side = v["side"]
        signals.append(Signal(
            day=day, bar_end=bar["bucket"] + timedelta(minutes=5), side=side,
            symbol=ce_sym if side == "call" else pe_sym, leader=v["confidence"],
            ladder=a["call_ladder"] if side == "call" else a["put_ladder"],
            pullback=pullback_entry(side, ce_ohlc if side == "call" else pe_ohlc),
        ))
    return signals, series


def walk(series: pd.DataFrame, fill_px: float, fill_idx: int, stop: float, target: float):
    """Walk 1-min candles from the fill bar. Returns (exit_price, reason, exit_ts)."""
    rows = series.iloc[fill_idx:]
    for n, (_, r) in enumerate(rows.iterrows()):
        if r["timestamp"].time() >= EOD_SQUAREOFF:
            return r["close"], "EOD", r["timestamp"]
        if r["low"] <= stop:
            return stop, "STOP", r["timestamp"]
        if n > 0 and r["high"] >= target:
            return target, "TARGET", r["timestamp"]
    last = rows.iloc[-1]
    return last["close"], "DATA_END", last["timestamp"]


def find_fill(series: pd.DataFrame, after: datetime, limit_px: float, kind: str):
    """First 1-min bar in the fill window where a breakout (high >= px) or a
    dip (low <= px) happens. Returns positional index or None."""
    end = after + timedelta(minutes=5 * FILL_WINDOW_BARS)
    win = series[(series["timestamp"] >= after) & (series["timestamp"] < end)]
    for pos, (_, r) in zip(win.index, win.iterrows()):
        if (kind == "breakout" and r["high"] >= limit_px) or (kind == "dip" and r["low"] <= limit_px):
            return series.index.get_loc(pos)
    return None


def simulate(day: date, signals: list[Signal], series: dict[str, pd.DataFrame], min_leader: int,
             variant: str) -> list[Trade]:
    trades: list[Trade] = []
    blocked_until: Optional[datetime] = None
    for s in signals:
        if s.leader < min_leader or (blocked_until and s.bar_end < blocked_until):
            continue
        ser = series[s.symbol]
        if variant.startswith("breakout"):
            fill_px = s.ladder["entry"]
            stop = s.ladder["stop_loss"]
            target = s.ladder["targets"][0 if variant.endswith("T1") else 1]["level"]
            pos = find_fill(ser, s.bar_end, fill_px, "breakout")
        else:
            pb = s.pullback
            if pb.get("wait") or not pb.get("zones"):
                continue
            fill_px = pb["zones"][{"pb-zone1": "zone1", "pb-zone2": "zone2", "pb-zone3": "zone3"}[variant]]
            stop, target = pb["stop_loss"], pb["targets"][0]["level"]
            pos = find_fill(ser, s.bar_end, fill_px, "dip")
        if pos is None:
            continue
        px, reason, ts = walk(ser, fill_px, pos, stop, target)
        if reason == "DATA_END":
            continue
        trades.append(Trade(day, variant, s.leader, (px - fill_px) * LOT_SIZE - COMMISSION, reason))
        blocked_until = ts
    return trades


def report(label: str, trades: list[Trade]) -> None:
    n = len(trades)
    if not n:
        print(f"  {label:<26} n=0")
        return
    wins = [t for t in trades if t.pnl > 0]
    gw = sum(t.pnl for t in wins)
    gl = abs(sum(t.pnl for t in trades if t.pnl <= 0))
    pf = gw / gl if gl else float("inf")
    tot = sum(t.pnl for t in trades)
    print(f"  {label:<26} n={n:<4} win={100*len(wins)/n:5.1f}%  pnl=Rs.{tot:>9,.0f}  PF={pf:4.2f}  avg=Rs.{tot/n:>6,.0f}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Study the analyzer score / pullback entry on history")
    ap.add_argument("dates", nargs="*")
    args = ap.parse_args()
    days = get_available_days(args.dates or None)
    print(f"{len(days)} days: {days[0]} .. {days[-1]}")

    per_day = []
    n_sig = 0
    for d in days:
        try:
            sigs, ser = collect_signals(d)
        except Exception as e:  # one bad day must not sink the study
            print(f"  {d}: skipped ({e})")
            continue
        per_day.append((d, sigs, ser))
        n_sig += len(sigs)
    print(f"{n_sig} analyzer YES signals across the sample")

    variants = ["breakout-T1", "breakout-T2", "pb-zone1", "pb-zone2", "pb-zone3"]
    for min_leader in (60, 75):
        print(f"\n== leader score >= {min_leader} ==")
        for v in variants:
            all_t: list[Trade] = []
            for d, sigs, ser in per_day:
                all_t.extend(simulate(d, sigs, ser, min_leader, v))
            report(v, all_t)

    print("\n== does the score carry edge? (breakout-T1, all YES signals, by leader score) ==")
    all_t = []
    for d, sigs, ser in per_day:
        all_t.extend(simulate(d, sigs, ser, 0, "breakout-T1"))
    for lo, hi in ((0, 65), (65, 80), (80, 101)):
        report(f"score {lo}-{hi - 1}", [t for t in all_t if lo <= t.leader < hi])

    print("\n== live engine baseline over the same days (exit at partial, MIN_RR gate on) ==")
    summarize("engine", run_backtest(days, "partial", LIVE_MIN_RR))


if __name__ == "__main__":
    main()
