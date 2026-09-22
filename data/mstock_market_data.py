"""
mStock Market Data Client — NOT currently wired into the live pipeline.

As of 2026-09-22, market data is AngelOne-only (data/market_data_adapter.py
directly, or the shared session in data/live_bars.py); mStock is order
execution only, via the separate broker/mstock_adapter.py. This file used to
be used by scripts/collect_ticks.py, strategy/premarket.py and
data/multi_source_market_data.py as a first-tried/primary market-data
source; that direction was reversed after a restart-storm bug (see
backend/app.py's _ensure_collector docstring) turned out to be starving
mStock's WebSocket via its single-session-per-login policy, and mStock's
own historical endpoint proved to have little practical benefit (it served
past sessions fine but nothing for the current session). Kept in place,
unused, in case dual-source market data is revisited later — nothing
currently imports MStockMarketData for market data.

Live ticks (WebSocket) + historical candles via mStock's Trading API (Type
A) — a sibling to data/market_data_adapter.py's AngelOne client, NOT a
modification of it. Exposes the same method shapes
(authenticate/fetch_historical_bars/fetch_last_n_bars/ws_connect/
ws_subscribe/ws_start_streaming/ws_stop_streaming/ws_disconnect).

Own, separate MConnect session from broker/mstock_adapter.py's order-
execution adapter — mirrors data/market_data_adapter.py's own AngelOne
session being separate from broker/angelone_adapter.py's, so a market-data
outage can't take down order execution or vice versa.

Ticks are normalized to the SAME dict shape AngelOne's
MarketDataAdapter._parse_ws_tick() produces (symbol, price, volume, oi,
bid_price, ask_price, timestamp), so collect_ticks.py's on_tick() handler
needs no source-specific branching.

IMPORTANT — confidence caveat: the instrument-master schema this file's
symbol resolution depends on (instrument_token, tradingsymbol, name,
expiry, strike, instrument_type, exchange) was confirmed directly against
the real cached master (data/jugaad_cache/mstock_instrument_master.json,
154,068 rows) — instrument_type is "CE"/"PE" directly, expiry is
"YYYY-MM-DD", strike is a numeric-parseable string, instrument_token is a
real field. get_historical_chart()'s response shape is now confirmed live
too (scripts/mstock_option_candle_check.py against a real NIFTY option
contract): {"status": "success", "data": {"candles": [[timestamp, open,
high, low, close, volume], ...]}} — one level deeper than first assumed
(candles nested inside "data", not "data" itself; the first live run
silently returned zero rows because of this, with no error raised). What
remains genuinely unverified: the WebSocket tick dict's exact field names
(last_price, volume_traded, open_interest, depth.bid/depth.ask,
last_traded_timestamp) beyond what's already been live-confirmed (see the
WS methods' own docstrings below for what's actually been proven).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Callable, Dict, List, Optional

import pandas as pd
import pyotp

from config.settings import (
    MSTOCK_API_KEY, MSTOCK_USER_ID, MSTOCK_PASSWORD, MSTOCK_TOTP_SECRET,
    MSTOCK_WS_URL,
)
from data.mstock_symbols import resolver as symbol_resolver
from utils.logger import get_logger

logger = get_logger("mstock_market_data")

# Same DB-internal option alias every other adapter in this project resolves
# — see data/market_data_adapter.py's _OPTION_ALIAS_RE for the canonical note.
_OPTION_ALIAS_RE = re.compile(r"^([A-Z]+)(\d{6})(\d+)(CE|PE)$")

_INTERVAL_MAP = {
    "1min": "minute", "3min": "3minute", "5min": "5minute",
    "10min": "10minute", "15min": "15minute", "30min": "30minute",
    "60min": "60minute", "day": "day",
}


def _failed_frame(reason) -> pd.DataFrame:
    """Same convention as data/market_data_adapter.py's _failed_frame() --
    an empty frame TAGGED as a failed request (rate limit, auth, upstream
    error), distinct from a bare empty frame meaning "genuinely no candle
    in this window." Callers (strategy/premarket.py's _FetchRefused
    detection, data/multi_source_market_data.py's failover) check
    df.attrs.get("error") to tell the two apart."""
    df = pd.DataFrame()
    df.attrs["error"] = str(reason) if reason else "request failed"
    return df


def _as_json(resp) -> dict:
    """Same SDK quirk as broker/mstock_adapter.py's _as_json() — MConnect
    calls return the raw requests.Response, not parsed JSON."""
    if resp is None:
        return {}
    if isinstance(resp, dict):
        return resp
    if hasattr(resp, "json"):
        try:
            return resp.json() or {}
        except ValueError:
            return {}
    return {}


class MStockMarketData:
    """AngelOne-market-data-adapter-shaped client for mStock's Type A API."""

    def __init__(self):
        self._mc = None
        self._access_token: Optional[str] = None
        self._authenticated = False

        self._ticker = None  # tradingapi_a.mticker.MTicker instance
        self._ws_connected = False
        self._callbacks: List[Callable] = []
        self._subscribed_tokens: Dict[str, str] = {}  # token(str) -> our symbol alias
        self._logged_sample_tick = False  # one-time raw-tick diagnostic log, see ws_start_streaming

    # ── Authentication ───────────────────────────────────────────────────

    def authenticate(self) -> bool:
        """
        Own mStock session, independent of broker/mstock_adapter.py's
        order-execution one. Non-interactive: TOTP secret lives in .env,
        pyotp computes the live 6-digit code at call time.
        """
        if self._authenticated and self._mc is not None:
            return True
        if not (MSTOCK_API_KEY and MSTOCK_USER_ID and MSTOCK_PASSWORD and MSTOCK_TOTP_SECRET):
            logger.error("mStock credentials not configured (MSTOCK_API_KEY/USER_ID/PASSWORD/TOTP_SECRET).")
            return False
        try:
            from tradingapi_a.mconnect import MConnect
        except ImportError as e:
            logger.error(f"mStock-TradingApi-A import failed: {e}. Run: pip install mStock-TradingApi-A")
            return False

        try:
            mc = MConnect()
            mc.set_api_key(MSTOCK_API_KEY)
            mc.login(MSTOCK_USER_ID, MSTOCK_PASSWORD)

            totp_code = pyotp.TOTP(MSTOCK_TOTP_SECRET).now()
            session = _as_json(mc.verify_totp(MSTOCK_API_KEY, totp_code))
            access_token = (session.get("data") or {}).get("access_token")

            if not access_token:
                logger.error(f"mStock market-data login failed: {session.get('message')}")
                return False

            mc.set_access_token(access_token)
            self._mc = mc
            self._access_token = access_token
            self._authenticated = True
            logger.info(f"mStock market-data session authenticated: {MSTOCK_USER_ID}")
            return True

        except Exception as e:
            logger.error(f"mStock market-data authentication failed: {e}")
            self._authenticated = False
            return False

    @property
    def is_authenticated(self) -> bool:
        return self._authenticated

    # ── Symbol Resolution Helper ─────────────────────────────────────────

    def _resolve(self, symbol: str, exchange: str) -> Optional[dict]:
        """Same alias-resolution contract as MarketDataAdapter._resolve() —
        see that method's docstring for the full rationale."""
        row = symbol_resolver.token_for(symbol, exchange, mconnect=self._mc)
        if row:
            return row
        underlying = symbol[:-2] if symbol.endswith("-I") else symbol
        if exchange == "NFO" and underlying in ("NIFTY", "BANKNIFTY", "FINNIFTY"):
            return symbol_resolver.current_futures_symbol(underlying, mconnect=self._mc)
        if exchange == "NFO":
            m = _OPTION_ALIAS_RE.match(symbol)
            if m:
                opt_underlying, exp_code, strike_str, opt_type = m.groups()
                try:
                    expiry = datetime.strptime(exp_code, "%y%m%d").date()
                except ValueError:
                    return None
                return symbol_resolver.option_symbol_for(
                    opt_underlying, expiry, float(strike_str), opt_type, exchange, mconnect=self._mc)
        return None

    # ── Historical: Candles ───────────────────────────────────────────────

    def fetch_historical_bars(
        self, symbol: str, start: datetime, end: datetime,
        interval: str = "1min", exchange: str = "NFO",
    ) -> pd.DataFrame:
        if not self.authenticate():
            return pd.DataFrame()

        row = self._resolve(symbol, exchange)
        if row is None:
            logger.error(f"Could not resolve mStock instrument token for {exchange}:{symbol}")
            return pd.DataFrame()

        token = row.get("instrument_token")  # confirmed live against the cached instrument master
        try:
            resp = _as_json(self._mc.get_historical_chart(
                exchange, str(token), _INTERVAL_MAP.get(interval, "minute"),
                start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S"),
            ))
            # Confirmed live (scripts/mstock_option_candle_check.py):
            # {"status": "success", "data": {"candles": [[ts,o,h,l,c,vol], ...]}}
            # -- one level deeper than first assumed (candles nested inside
            # "data", not "data" itself). An error envelope has no "data"
            # key at all ("message"/"error_type" instead).
            data_obj = resp.get("data")
            if data_obj is None:
                msg = resp.get("message") or resp.get("error_type") or "no data field in mStock response"
                logger.error(f"mStock getHistoricalChart failed for {symbol}: {msg}")
                return _failed_frame(msg)
            rows = data_obj.get("candles") if isinstance(data_obj, dict) else data_obj
            if not rows:
                return pd.DataFrame()  # genuinely no candle in this window

            df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_localize(None)
            # Store under the caller's stable alias (e.g. "NIFTY-I"), matching
            # MarketDataAdapter.fetch_historical_bars()'s convention.
            df["symbol"] = symbol
            df["oi"] = 0

            logger.info(f"mStock: fetched {len(df)} {interval} bars for {symbol}")
            return df

        except Exception as e:
            logger.error(f"mStock historical fetch failed for {symbol}: {e}")
            return _failed_frame(e)

    def fetch_last_n_bars(
        self, symbol: str, n: int = 200, interval: str = "1min", exchange: str = "NFO",
    ) -> pd.DataFrame:
        from datetime import timedelta
        end = datetime.now()
        # A flat 5-day window at interval="1min" implies well over 1000 candles, which mStock's
        # getHistoricalChart rejects outright ("Requested data exceeds the maximum limit of 1000 candles") --
        # confirmed live: every collect_ticks.py startup hit this on its n=1 NIFTY-I reference-price call
        # (2026-09-18, 2026-09-22), silently falling back to AngelOne and adding ~1s of dead time to every
        # collector (re)start. data/multi_source_market_data.py's MultiSourceMarketData.fetch_last_n_bars()
        # already sizes its window this same safe way -- match it here for the class that method wraps.
        if interval == "day":
            lookback_days = n + 5  # n is already in days; +5 covers weekends/holidays
        else:
            lookback_days = max(1, (n // 375) + 2)  # ~375 minute-bars/trading day, +2 covers a weekend gap
        start = end - timedelta(days=lookback_days)
        df = self.fetch_historical_bars(symbol, start, end, interval, exchange)
        return df.tail(n).reset_index(drop=True) if not df.empty else df

    # ── Live: WebSocket Ticks ─────────────────────────────────────────────

    def ws_connect(self) -> bool:
        if not self.authenticate():
            return False
        try:
            from tradingapi_a.mticker import MTicker
        except ImportError as e:
            # mticker.py pulls in Twisted's TLS stack (pyOpenSSL,
            # service_identity), which pip installing mStock-TradingApi-A
            # does NOT bring along as a dependency — confirmed live: the
            # generic "not installed" message here hid a real
            # ModuleNotFoundError for 'OpenSSL' (then 'service_identity').
            logger.error(
                f"mStock-TradingApi-A mticker import failed: {e}. "
                f"Run: pip install mStock-TradingApi-A pyOpenSSL service_identity"
            )
            return False

        try:
            self._ticker = MTicker(MSTOCK_API_KEY, self._access_token, MSTOCK_WS_URL)
            return True
        except Exception as e:
            logger.error(f"mStock ticker init failed: {e}")
            return False

    def ws_subscribe(self, symbols: List[str], exchange: str = "NFO"):
        """Resolve each symbol to mStock's instrument token and remember the
        reverse mapping for _parse_ws_tick(). Actual wire subscribe happens
        in ws_start_streaming()'s on_connect (and here too, if already
        connected, to support adding symbols mid-session like AngelOne's
        dynamic ATM re-subscription in collect_ticks.py)."""
        newly_resolved = []
        for sym in symbols:
            row = self._resolve(sym, exchange)
            if row is None:
                logger.warning(f"mStock: could not resolve token for {sym}, skipping")
                continue
            token = str(row.get("instrument_token"))
            self._subscribed_tokens[token] = sym
            newly_resolved.append(token)

        if self._ticker is not None and self._ws_connected and newly_resolved:
            tokens = [int(t) for t in newly_resolved if t.isdigit()]
            if tokens:
                self._ticker.subscribe(tokens)
                self._ticker.set_mode(self._ticker.MODE_FULL, tokens)

    def ws_start_streaming(self, callback: Callable):
        self._callbacks.append(callback)
        if self._ticker is None:
            logger.error("mStock ticker not connected; call ws_connect() first")
            return

        def on_connect(ws, response):
            # Required per the official SDK's own usage example — omitting
            # this was the bug the first live test hit: the server closed
            # the connection (code 1000) within ~1s of every connect,
            # because it never received this login frame and treated the
            # stream as unauthenticated.
            self._ticker.send_login_after_connect()
            tokens = [int(t) for t in self._subscribed_tokens if t.isdigit()]
            if tokens:
                ws.subscribe(tokens)
                ws.set_mode(self._ticker.MODE_FULL, tokens)
            self._ws_connected = True
            logger.info(f"mStock WS connected, subscribed {len(tokens)} tokens")

        def on_ticks(ws, ticks):
            for raw in ticks:
                # Log the very first raw tick verbatim, unconditionally —
                # the docs-summary field shapes I built _parse_ws_tick()
                # from didn't match a real packet (open_interest turned out
                # to be a tuple, not a plain int), and guessing again blind
                # just repeats the same trial-and-error. Ground truth once,
                # then fix precisely instead of guessing a third time.
                if not self._logged_sample_tick:
                    logger.info(f"mStock RAW TICK SAMPLE: {raw!r}")
                    self._logged_sample_tick = True
                try:
                    parsed = self._parse_ws_tick(raw)
                except Exception as e:
                    logger.error(f"mStock tick parse error: {e} — raw={raw!r}")
                    continue
                if parsed is None:
                    continue
                for cb in self._callbacks:
                    try:
                        cb(parsed)
                    except Exception as e:
                        logger.error(f"mStock tick callback error: {e}")

        def on_close(ws, code, reason):
            self._ws_connected = False
            logger.warning(f"mStock WS closed: {code} {reason}")

        def on_reconnect(ws, attempt, delay):
            logger.warning(f"mStock WS reconnecting (attempt {attempt}, delay {delay}s)")

        self._ticker.on_connect = on_connect
        self._ticker.on_ticks = on_ticks
        self._ticker.on_close = on_close
        self._ticker.on_reconnect = on_reconnect
        self._ticker.connect(threaded=True)

    @staticmethod
    def _num(v, kind=float, default=0):
        """
        Defensively coerce a tick field to a number. Confirmed live that at
        least one field (open_interest) doesn't arrive as a plain scalar —
        parsing it as int() directly raised "int() argument must be ...
        not 'tuple'". Rather than special-case every field once the real
        shape is known (from the RAW TICK SAMPLE log line in
        ws_start_streaming), unwrap any list/tuple to its first element and
        fall back to `default` on anything still not coercible, so a
        surprising field type degrades a value to 0 instead of dropping
        the whole tick.
        """
        if isinstance(v, (list, tuple)):
            v = v[0] if v else default
        if isinstance(v, dict):
            v = v.get("value", default)
        try:
            return kind(v)
        except (TypeError, ValueError):
            return default

    def _parse_ws_tick(self, raw: dict) -> Optional[dict]:
        """Normalize mStock's tick dict to AngelOne's _parse_ws_tick() shape.
        VERIFY: field names (last_price, volume_traded, open_interest,
        depth.bid/depth.ask, last_traded_timestamp) are from the docs
        summary, not a live packet — check the RAW TICK SAMPLE log line
        ws_start_streaming() emits and adjust here if a real tick differs."""
        token = str(raw.get("instrument_token", ""))
        symbol = self._subscribed_tokens.get(token)
        if symbol is None:
            return None

        price = self._num(raw.get("last_price", 0))
        depth = raw.get("depth") or {}
        bid_levels = depth.get("bid") or []
        ask_levels = depth.get("ask") or []
        bid_top = bid_levels[0] or {} if bid_levels else {}
        ask_top = ask_levels[0] or {} if ask_levels else {}
        bid = self._num(bid_top.get("price", price))
        ask = self._num(ask_top.get("price", price))

        return {
            "symbol": symbol,
            "price": price,
            # mStock's own last_traded_timestamp/exchange_timestamp arrive
            # as a "%Y-%m-%dT%I:%M:%S%p" string (confirmed live) — deliberately
            # NOT parsed. MarketDataAdapter._parse_ws_tick() drops AngelOne's
            # equivalent exchange timestamp the same way ("wall-clock kept
            # for parity") because the rest of the pipeline (live price
            # cache freshness checks, on_tick()'s minute-candle bucketing)
            # is built around receipt time, not exchange time — matching
            # that convention here rather than introducing a second one.
            "volume": self._num(raw.get("volume_traded", 0), kind=int),
            "oi": self._num(raw.get("open_interest", 0), kind=int),
            "bid_price": bid,
            "ask_price": ask,
            # Confirmed live in the depth packet's top-of-book level
            # ("quantity" key, alongside "price"/"orders"/"padding") —
            # previously omitted entirely, which was the actual trigger for
            # a market-hours bug: AngelOne's parser always sets bid_qty/
            # ask_qty, so a tick buffer mixing sources had this column as a
            # mix of real ints and Python None. pandas silently upcasts
            # that mix to NaN, and tick_data's bid_qty/ask_qty are BIGINT —
            # Postgres rejected NaN as "out of range" on every flush,
            # dropping the whole batch. Fixed defensively either way in
            # data/tick_collector.py's flush(), but populating the real
            # figure here is strictly better than leaving it None.
            "bid_qty": self._num(bid_top.get("quantity", 0), kind=int),
            "ask_qty": self._num(ask_top.get("quantity", 0), kind=int),
            "timestamp": datetime.now(),
        }

    def ws_stop_streaming(self):
        self._callbacks.clear()

    def ws_disconnect(self):
        if self._ticker is not None:
            try:
                self._ticker.close()
            except Exception as e:
                logger.debug(f"mStock ticker close error (ignoring): {e}")
        self._ticker = None
        self._ws_connected = False

    @property
    def is_ws_connected(self) -> bool:
        return self._ws_connected
