"""
One-off exporter: copy the 5-minute bars a RUNNING backend already holds in memory into the on-disk cache
(data/intraday_cache/bars/<SYMBOL>.csv.gz) so intraday research can be repeated offline.

It only calls the backend's own /api/intraday/chart endpoint -- no broker login, no credentials, no extra
API load. Sessions still forming (today, while the market is open) are dropped: a research file must only
contain completed sessions.

    python scripts/export_intraday_bars.py            # all universe symbols + NIFTY-I, last 12 sessions

Once strategy/intraday_service persists bars itself (every sync), this is only useful to seed the cache from
a backend that is still running an older build.
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path
from urllib.request import urlopen
from urllib.parse import quote

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.intraday_bars_store import save_bars  # noqa: E402

API = "http://localhost:5050"
YEAR = date.today().year


def fetch(symbol: str, sessions: int) -> pd.DataFrame | None:
    try:
        d = json.loads(urlopen(f"{API}/api/intraday/chart?symbol={quote(symbol)}&sessions={sessions}", timeout=30).read())
    except Exception as e:
        print(f"  {symbol}: {e}")
        return None
    if "times" not in d:
        print(f"  {symbol}: {d.get('error', 'no data')}")
        return None
    ts = [datetime.strptime(f"{YEAR} {t}", "%Y %d %b %H:%M") for t in d["times"]]
    return pd.DataFrame({"timestamp": ts, "open": d["open"], "high": d["high"], "low": d["low"], "close": d["close"], "volume": d["volume"]})


def main() -> None:
    uni_file = Path(__file__).resolve().parent.parent / "data" / "intraday_cache" / f"universe_{date.today().isoformat()}.json"
    syms = json.loads(uni_file.read_text(encoding="utf-8")) if uni_file.exists() else []
    if not syms:
        from strategy.intraday_service import FALLBACK_UNIVERSE
        syms = FALLBACK_UNIVERSE
    sessions = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    ok = 0
    for s in list(syms) + ["NIFTY-I"]:
        df = fetch(s, sessions)
        if df is None or df.empty:
            continue
        # drop a session that is still forming (fewer than a full day of bars AND it is today)
        today = pd.Timestamp(date.today())
        day = df["timestamp"].dt.normalize()
        counts = df.groupby(day)["timestamp"].transform("count")
        df = df[~((day == today) & (counts < 70))]
        n = save_bars(s, df, merge=False)
        ok += 1
        print(f"  {s:12} {n:5d} bars, {df['timestamp'].dt.date.nunique()} sessions")
    print(f"exported {ok}/{len(syms) + 1} symbols")


if __name__ == "__main__":
    main()
