"""
Multi-Source Market Data — mStock primary, AngelOne fallback

NOT currently used. As of 2026-09-22 every former caller
(scripts/backfill_today.py, backfill_history.py, backfill_option_days.py,
fetch_missing_ticks.py) constructs data/market_data_adapter.py's
MarketDataAdapter directly instead -- market data is AngelOne-only now,
mStock is order execution only (broker/mstock_adapter.py). See
data/mstock_market_data.py's module docstring for why the mstock-primary
direction was reversed. Kept in place, unused, in case dual-source market
data is revisited later.
───────────────────────────────────────────────────────────
A drop-in replacement for data/market_data_adapter.py's MarketDataAdapter,
same public interface (authenticate/fetch_historical_bars/
fetch_last_n_bars), for callers that want automatic failover instead of
picking one broker directly.

Tries mStock (data/mstock_market_data.py's MStockMarketData) first, per
the user's former "mstock as primary" direction; falls back to AngelOne
(data/market_data_adapter.py's MarketDataAdapter) on any failure --
mStock's historical-candle REST path is newer and less proven than
AngelOne's, so a clean fallback matters here more than it would the other
way around. Every returned DataFrame carries df.attrs["source"] =
"mstock"|"angelone" so callers/logs can tell which one actually served a
given request.

Symbol format: both adapters' own _resolve() already accept the DB-internal
option alias ("NIFTY26090823750PE", see backtest/option_resolver.py's
build_option_symbol()) via the same _OPTION_ALIAS_RE pattern, and both
resolve a bare index alias ("NIFTY-I") to the current futures contract --
this class does no resolution of its own, it just tries each adapter's
existing fetch_historical_bars() with the caller's symbol unchanged.

NOT used by strategy/premarket.py's live agent path -- that one resolves
AngelOne's own live tradingsymbol via data.angelone_symbols.resolver
specifically to avoid a documented bug where the DB-alias format silently
fails for today's freshest/still-forming contracts (see
_resolve_atm_symbols()'s own docstring). Reusing this alias-based wrapper
there would reintroduce that exact failure mode for the AngelOne leg, so
that call site gets its own small dual-fetch instead (see
strategy/premarket.py's _fetch_option_candle()).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from data.market_data_adapter import MarketDataAdapter
from data.mstock_market_data import MStockMarketData
from utils.logger import get_logger

logger = get_logger("multi_source_market_data")


class MultiSourceMarketData:
    def __init__(self):
        self._mstock = MStockMarketData()
        self._angelone = MarketDataAdapter()

    def authenticate(self) -> bool:
        """True if EITHER source is usable -- fetch_historical_bars() tries
        each independently anyway, so this is just a cheap up-front check
        for callers that want to fail fast when both are down."""
        mstock_ok = self._mstock.authenticate()
        angelone_ok = self._angelone.authenticate()
        if not mstock_ok:
            logger.warning("mStock market-data session unavailable -- calls will fall back to AngelOne")
        if not angelone_ok:
            logger.warning("AngelOne market-data session unavailable -- calls depend on mStock alone")
        return mstock_ok or angelone_ok

    def fetch_historical_bars(
        self, symbol: str, start: datetime, end: datetime,
        interval: str = "1min", exchange: str = "NFO",
    ) -> pd.DataFrame:
        df = self._mstock.fetch_historical_bars(symbol, start, end, interval, exchange)
        if not df.empty:
            df.attrs["source"] = "mstock"
            return df
        if df.attrs.get("error"):
            logger.warning(f"mStock refused {symbol} ({df.attrs['error']}) -- falling back to AngelOne")
        # An empty, untagged frame from mStock ("genuinely no candle in
        # this window") is also worth trying AngelOne for -- the two
        # brokers' historical coverage can differ (e.g. illiquid-strike
        # collection gaps), and a second opinion costs one extra call.
        df = self._angelone.fetch_historical_bars(symbol, start, end, interval, exchange)
        if not df.empty:
            df.attrs["source"] = "angelone"
        return df

    def fetch_last_n_bars(
        self, symbol: str, n: int = 200, interval: str = "1min", exchange: str = "NFO",
    ) -> pd.DataFrame:
        """Same lookback-window approach as MarketDataAdapter's own
        fetch_last_n_bars() -- reuses THIS class's fetch_historical_bars()
        so the mstock-then-angelone failover applies here too."""
        end = datetime.now()
        lookback_days = max(1, (n // 375) + 2)
        df = self.fetch_historical_bars(symbol, end - timedelta(days=lookback_days), end, interval, exchange)
        source = df.attrs.get("source")
        df = df.tail(n).reset_index(drop=True) if not df.empty else df
        if source:
            df.attrs["source"] = source
        return df

    def fetch_historical_ticks(self, symbol: str, **kwargs) -> pd.DataFrame:
        """Neither broker has a historical tick-level endpoint (AngelOne's
        free-tier gap is documented; mStock doesn't even stub the method) --
        no failover to build here. Pure passthrough to AngelOne's existing
        always-empty stub, kept only so callers that share one adapter
        object across both tick and candle fetches (e.g.
        scripts/fetch_missing_ticks.py) don't need a second instance."""
        return self._angelone.fetch_historical_ticks(symbol, **kwargs)
