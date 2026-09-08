"""
Market Data Adapter (AngelOne SmartAPI + jugaad-data)
───────────────────────────────────────────────────────
Replaces the paid TrueData feed. Same public method surface as the old
`TrueDataAdapter` (`authenticate`, `fetch_historical_bars`,
`fetch_historical_minute_bars`, `fetch_historical_ticks`, `fetch_last_n_bars`,
`fetch_last_n_ticks`, `fetch_bhavcopy`, `ws_connect`, `ws_subscribe`,
`ws_unsubscribe`, `ws_start_streaming`, `ws_stop_streaming`, `ws_disconnect`,
`fetch_all_historical`, `disconnect`, plus the `tcp_*` aliases) so every
caller (`scripts/collect_ticks.py`, `data/tick_collector.py`,
`backtest/option_resolver.py`, the backfill scripts) only needed an import
change, not a rewrite.

Two backends, split by what each is actually good for:

1. **AngelOne SmartAPI** — live ticks (websocket) + historical minute/day
   candles (REST `getCandleData`). Free for account holders, no separate API
   subscription fee (unlike Kite Connect). Requires `ANGEL_API_KEY`,
   `ANGEL_CLIENT_CODE`, `ANGEL_PASSWORD_OR_PIN`, `ANGEL_TOTP_SECRET` in `.env`
   — see `scripts/angelone_check_auth.py` for a no-secrets-in-chat way to
   verify these work.

2. **jugaad-data** — free EOD bhavcopy (`fetch_bhavcopy`). No auth needed.

Known gap (documented in the project plan): AngelOne's free tier has no
historical *tick*-level API. `fetch_historical_ticks` / `fetch_last_n_ticks`
return an empty DataFrame — callers already handle sparse tick data by
falling back to minute candles (see `scripts/tick_replay_backtest.py`).
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional

import pandas as pd
import pyotp
import requests

from config.settings import (
    ANGEL_API_KEY,
    ANGEL_CLIENT_CODE,
    ANGEL_PASSWORD_OR_PIN,
    ANGEL_TOTP_SECRET,
    ANGEL_WS_URL,
)
from data.angelone_symbols import resolver as symbol_resolver
from utils.logger import get_logger

logger = get_logger("market_data")

# DB-internal option symbol alias (see CLAUDE.md's "Symbol Naming" section
# and backtest/option_resolver.py::build_option_symbol): "NIFTY{yymmdd}{strike}{CE|PE}"
# e.g. "NIFTY26090823900CE". NOT a real AngelOne tradingsymbol — AngelOne's
# actual format is "NIFTY{DD}{MMM}{YY}{strike}{CE|PE}" (e.g.
# "NIFTY08SEP2623900CE"). _resolve() below translates this alias the same
# way it already translates "NIFTY-I" to a real futures contract.
_OPTION_ALIAS_RE = re.compile(r"^([A-Z]+)(\d{6})(\d+)(CE|PE)$")


def _failed_frame(reason) -> pd.DataFrame:
    """
    An empty frame TAGGED as a failed request.

    A refusal (rate limit, auth failure, upstream error) and "this window
    genuinely has no candle" both used to come back as a bare empty frame,
    which callers cannot tell apart. Date walk-back loops therefore read a
    rate-limit refusal as "nothing traded that day", stepped back another
    day, and burned another request — compounding the throttle and
    eventually returning a candle from days earlier as if it were current.
    Callers that care check `df.attrs.get("error")`; everything else keeps
    treating it as empty, exactly as before. Same `.attrs` convention
    backtest/option_resolver.py already uses for `_mode`.
    """
    df = pd.DataFrame()
    df.attrs["error"] = str(reason) if reason else "request failed"
    return df

_INTERVAL_MAP = {
    "1min": "ONE_MINUTE",
    "3min": "THREE_MINUTE",
    "5min": "FIVE_MINUTE",
    "15min": "FIFTEEN_MINUTE",
    "30min": "THIRTY_MINUTE",
    "60min": "ONE_HOUR",
    "eod": "ONE_DAY",
}


class MarketDataAdapter:
    """
    AngelOne (live ticks + historical candles) + jugaad-data (EOD bhavcopy)
    adapter. See module docstring for the split.
    """

    def __init__(self):
        self._smart = None  # SmartApi.SmartConnect instance
        self._jwt_token: Optional[str] = None
        self._feed_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._authenticated: bool = False

        self._ws = None  # SmartApi.smartWebSocketV2.SmartWebSocketV2 instance
        self._ws_connected: bool = False
        self._callbacks: List[Callable] = []
        self._streaming: bool = False
        self._stream_thread: Optional[threading.Thread] = None
        self._subscribed_tokens: Dict[str, str] = {}  # token -> symbol name

    # ── Authentication ───────────────────────────────────────────────────────

    def authenticate(self) -> bool:
        """
        Log in to AngelOne SmartAPI. Fully non-interactive: the TOTP secret
        (not a one-time code) lives in `.env`, and `pyotp` computes the
        current 6-digit code at call time.
        """
        if self._authenticated and self._smart is not None:
            return True

        if not (ANGEL_API_KEY and ANGEL_CLIENT_CODE and ANGEL_PASSWORD_OR_PIN and ANGEL_TOTP_SECRET):
            logger.error("AngelOne credentials not configured (ANGEL_API_KEY/ANGEL_CLIENT_CODE/"
                         "ANGEL_PASSWORD_OR_PIN/ANGEL_TOTP_SECRET).")
            return False

        try:
            from SmartApi import SmartConnect
        except ImportError:
            logger.error("smartapi-python not installed. Run: pip install smartapi-python")
            return False

        try:
            self._smart = SmartConnect(api_key=ANGEL_API_KEY)
            totp = pyotp.TOTP(ANGEL_TOTP_SECRET).now()
            session = self._smart.generateSession(ANGEL_CLIENT_CODE, ANGEL_PASSWORD_OR_PIN, totp)

            if not session.get("status"):
                logger.error(f"AngelOne login failed: {session.get('message')}")
                return False

            data = session["data"]
            self._jwt_token = data["jwtToken"]
            self._refresh_token = data["refreshToken"]
            self._feed_token = self._smart.getfeedToken()
            self._authenticated = True
            logger.info(f"AngelOne authenticated: client={ANGEL_CLIENT_CODE}")
            return True

        except Exception as e:
            logger.error(f"AngelOne authentication failed: {e}")
            self._authenticated = False
            return False

    @property
    def is_authenticated(self) -> bool:
        return self._authenticated

    # ── Symbol Resolution Helper ─────────────────────────────────────────────

    def _resolve(self, symbol: str, exchange: str) -> Optional[dict]:
        """
        Resolve a symbol to its AngelOne instrument row.

        The whole DB schema / dashboard / ML pipeline is keyed on two
        synthetic aliases rather than AngelOne's own tradingsymbol strings —
        rather than touch 50+ call sites, both resolve here:

          - "NIFTY-I" / "BANKNIFTY-I" / "FINNIFTY-I" (continuous-futures
            alias) -> AngelOne's actual current front-month futures contract.
          - "NIFTY{yymmdd}{strike}{CE|PE}" (DB-internal option alias, see
            _OPTION_ALIAS_RE above) -> AngelOne's actual option tradingsymbol
            via the instrument master's structured fields (name/expiry/
            strike/instrumenttype), since guessing AngelOne's real string
            format ("NIFTY08SEP2623900CE") is unreliable — confirmed by
            querying the live instrument master directly: the yymmdd-style
            alias never matches a real row, which silently broke every live
            caller of an option symbol (websocket subscribe in
            collect_ticks.py, and REST backfill here) until this fix.

        Callers keep writing/reading DB rows under the alias — only the
        AngelOne API calls (getCandleData, websocket subscribe) need the
        real symbol/token, resolved transparently here.
        """
        row = symbol_resolver.token_for(symbol, exchange)
        if row:
            return row
        underlying = symbol[:-2] if symbol.endswith("-I") else symbol
        if exchange == "NFO" and underlying in ("NIFTY", "BANKNIFTY", "FINNIFTY"):
            return symbol_resolver.current_futures_symbol(underlying)
        if exchange == "NFO":
            m = _OPTION_ALIAS_RE.match(symbol)
            if m:
                opt_underlying, exp_code, strike_str, opt_type = m.groups()
                try:
                    expiry = datetime.strptime(exp_code, "%y%m%d").date()
                except ValueError:
                    return None
                return symbol_resolver.option_symbol_for(opt_underlying, expiry, float(strike_str), opt_type)
        return None

    # ── Historical: Candles ───────────────────────────────────────────────────

    def fetch_historical_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        interval: str = "1min",
        exchange: str = "NFO",
    ) -> pd.DataFrame:
        """
        Fetch historical OHLCV candles via AngelOne's getCandleData.

        Args:
            symbol: AngelOne tradingsymbol, or a bare index underlying
                (resolved to the current futures contract).
            interval: 1min/3min/5min/15min/30min/60min/eod
        """
        if not self.authenticate():
            return pd.DataFrame()

        row = self._resolve(symbol, exchange)
        if row is None:
            logger.error(f"Could not resolve symbol token for {exchange}:{symbol}")
            return pd.DataFrame()

        params = {
            "exchange": row["exch_seg"],
            "symboltoken": row["token"],
            "interval": _INTERVAL_MAP.get(interval, "ONE_MINUTE"),
            "fromdate": start.strftime("%Y-%m-%d %H:%M"),
            "todate": end.strftime("%Y-%m-%d %H:%M"),
        }

        try:
            resp = self._smart.getCandleData(params)
            if not resp.get("status"):
                logger.error(f"getCandleData failed for {symbol}: {resp.get('message')}")
                return _failed_frame(resp.get("message"))

            rows = resp.get("data", [])
            if not rows:
                return pd.DataFrame()  # genuinely no candle in this window

            df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_localize(None)
            # Store under the caller's symbol (e.g. "NIFTY-I"), not AngelOne's
            # actual current-contract name — the DB/dashboard/ML pipeline are
            # all keyed on the stable synthetic symbol, see _resolve().
            df["symbol"] = symbol
            df["oi"] = 0  # candle API doesn't return OI; use option-chain snapshot for that

            logger.info(f"Fetched {len(df)} {interval} bars for {symbol}")
            return df

        except Exception as e:
            logger.error(f"Error fetching bars for {symbol}: {e}")
            return _failed_frame(e)

    def fetch_historical_minute_bars(
        self,
        symbol: str,
        days: int = 180,
        end_date: Optional[datetime] = None,
        exchange: str = "NFO",
    ) -> pd.DataFrame:
        """
        Fetch historical 1-minute bars (convenience wrapper). Powers the
        Macro ML Model training. Chunked into ~30-day windows — AngelOne's
        getCandleData caps how many candles a single request can return.
        """
        end_date = end_date or datetime.now()
        start_date = end_date - timedelta(days=days)

        logger.info(f"Fetching 1m bars for {symbol}: {start_date.date()} → {end_date.date()} (chunked)")

        chunks: list[pd.DataFrame] = []
        chunk_start = start_date
        while chunk_start < end_date:
            chunk_end = min(chunk_start + timedelta(days=30), end_date)
            df = self.fetch_historical_bars(symbol, chunk_start, chunk_end, "1min", exchange)
            if not df.empty:
                chunks.append(df)
            chunk_start = chunk_end
            time.sleep(0.35)  # AngelOne historical API: ~3 req/sec limit

        if not chunks:
            return pd.DataFrame()

        combined = pd.concat(chunks, ignore_index=True)
        combined.drop_duplicates(subset=["timestamp"], keep="last", inplace=True)
        combined.sort_values("timestamp", inplace=True)
        combined.reset_index(drop=True, inplace=True)

        logger.info(f"Combined {len(chunks)} chunks → {len(combined)} bars for {symbol}")
        return combined

    def fetch_last_n_bars(
        self, symbol: str, n: int = 200, interval: str = "1min", exchange: str = "NFO"
    ) -> pd.DataFrame:
        """Fetch the last N bars by requesting a generous lookback window and tailing it."""
        end = datetime.now()
        lookback_days = max(1, (n // 375) + 2)  # ~375 one-minute bars per trading day
        df = self.fetch_historical_bars(symbol, end - timedelta(days=lookback_days), end, interval, exchange)
        return df.tail(n).reset_index(drop=True) if not df.empty else df

    # ── Historical: Ticks (unavailable on the free tier) ─────────────────────

    def fetch_historical_ticks(
        self, symbol: str, start: Optional[datetime] = None, end: Optional[datetime] = None,
        days: int = 5, bidask: bool = True,
    ) -> pd.DataFrame:
        """
        No free historical tick-level API exists for AngelOne. Returns an
        empty DataFrame — callers already fall back to minute candles when
        tick data is sparse (see tick_replay_backtest.py).
        """
        logger.debug(f"fetch_historical_ticks({symbol}): no free tick history source, returning empty")
        return pd.DataFrame()

    def fetch_last_n_ticks(self, symbol: str, n: int = 200, bidask: bool = True) -> pd.DataFrame:
        logger.debug(f"fetch_last_n_ticks({symbol}): no free tick history source, returning empty")
        return pd.DataFrame()

    # ── Historical: Bhavcopy (jugaad-data) ────────────────────────────────────

    def fetch_bhavcopy(self, segment: str = "FO", date_str: Optional[str] = None) -> pd.DataFrame:
        """
        Fetch EOD bhavcopy via jugaad-data (free, no auth).
        segment: "FO" (futures & options) or "EQ" (equity/index cash market).
        """
        from data.jugaad_adapter import fetch_bhavcopy as jugaad_bhavcopy

        dt = datetime.strptime(date_str, "%Y-%m-%d").date() if date_str else datetime.now().date()
        df = jugaad_bhavcopy(dt, segment=segment)
        logger.info(f"Fetched bhavcopy for {segment} on {dt}: {len(df)} rows")
        return df

    # ── WebSocket: Real-time Streaming ────────────────────────────────────────

    def ws_connect(self) -> bool:
        """Connect to AngelOne's SmartWebSocketV2 live feed."""
        if not self.authenticate():
            return False

        try:
            from SmartApi.smartWebSocketV2 import SmartWebSocketV2
        except ImportError:
            logger.error("smartapi-python not installed. Run: pip install smartapi-python")
            return False

        try:
            self._ws = SmartWebSocketV2(
                self._jwt_token, ANGEL_API_KEY, ANGEL_CLIENT_CODE, self._feed_token,
            )
            self._ws.on_data = self._on_ws_data
            self._ws.on_error = lambda ws, err: logger.error(f"AngelOne WS error: {err}")
            self._ws.on_close = lambda ws: logger.warning("AngelOne WS closed")

            def _connect_blocking():
                self._ws.connect()

            threading.Thread(target=_connect_blocking, daemon=True).start()
            time.sleep(2)  # let the connect handshake complete
            self._ws_connected = True
            logger.info("AngelOne WebSocket connected")
            return True

        except Exception as e:
            logger.error(f"AngelOne WebSocket connection failed: {e}")
            self._ws_connected = False
            return False

    def ws_subscribe(self, symbols: List[str], exchange: str = "NFO"):
        """
        Subscribe to symbols on the live feed. Resolves each symbol to its
        AngelOne token first (see `_resolve`).
        """
        if not self._ws_connected:
            logger.error("WebSocket not connected. Call ws_connect() first.")
            return

        exch_type_map = {"NSE": 1, "NFO": 2, "BSE": 3}
        tokens = []
        for sym in symbols:
            row = self._resolve(sym, exchange)
            if row is None:
                logger.warning(f"Skipping unresolvable symbol: {sym}")
                continue
            # Map token -> the caller's symbol (e.g. "NIFTY-I"), not AngelOne's
            # actual contract name — see the comment in fetch_historical_bars.
            self._subscribed_tokens[row["token"]] = sym
            tokens.append(row["token"])

        if not tokens:
            return

        token_list = [{"exchangeType": exch_type_map.get(exchange, 2), "tokens": tokens}]
        self._ws.subscribe("ai-trader", 3, token_list)  # mode 3 = SNAP_QUOTE (LTP+depth+OI)
        logger.info(f"WebSocket subscribed to {len(tokens)} symbols: {symbols[:5]}{'...' if len(symbols) > 5 else ''}")

    def ws_unsubscribe(self, symbols: List[str], exchange: str = "NFO"):
        if not self._ws_connected:
            return
        exch_type_map = {"NSE": 1, "NFO": 2, "BSE": 3}
        tokens = [self._resolve(sym, exchange)["token"] for sym in symbols if self._resolve(sym, exchange)]
        if tokens:
            self._ws.unsubscribe("ai-trader", 3, [{"exchangeType": exch_type_map.get(exchange, 2), "tokens": tokens}])
            logger.info(f"WebSocket unsubscribed from {len(tokens)} symbols")

    def ws_start_streaming(self, callback: Callable):
        """
        Register a tick callback. AngelOne's SDK drives `on_data` itself
        once `.connect()` is running (started in `ws_connect`), so this just
        registers the callback rather than spinning up its own loop.

        Callback receives the same tick dict shape the old TrueData adapter
        produced: {symbol, symbol_id, timestamp, price, volume, atp,
        total_volume, open, high, low, prev_close, oi, prev_oi, turnover,
        bid_price, bid_qty, ask_price, ask_qty}.
        """
        if callback not in self._callbacks:
            self._callbacks.append(callback)
        self._streaming = True
        logger.info("AngelOne tick callback registered")

    def _on_ws_data(self, wsapp, message: dict):
        """SmartWebSocketV2's on_data handler — normalizes and fans out ticks."""
        tick = self._parse_ws_tick(message)
        if tick is None:
            return
        for cb in self._callbacks:
            try:
                cb(tick)
            except Exception as e:
                logger.error(f"Callback error: {e}")

    def _parse_ws_tick(self, msg: dict) -> Optional[dict]:
        """Normalize an AngelOne SNAP_QUOTE message into the shared tick shape."""
        try:
            token = str(msg.get("token", ""))
            best_bid = (msg.get("best_5_buy_data") or [{}])[0]
            best_ask = (msg.get("best_5_sell_data") or [{}])[0]
            return {
                "symbol": self._subscribed_tokens.get(token, token),
                "symbol_id": int(token) if token.isdigit() else 0,
                "timestamp": datetime.now(),  # Angel sends exchange_timestamp in ms; wall-clock kept for parity
                "price": float(msg.get("last_traded_price", 0)) / 100,  # Angel prices are paise-scaled
                "volume": int(msg.get("last_traded_quantity", 0)),
                "atp": float(msg.get("average_traded_price", 0)) / 100,
                "total_volume": int(msg.get("volume_trade_for_the_day", 0)),
                "open": float(msg.get("open_price_of_the_day", 0)) / 100,
                "high": float(msg.get("high_price_of_the_day", 0)) / 100,
                "low": float(msg.get("low_price_of_the_day", 0)) / 100,
                "prev_close": float(msg.get("closed_price", 0)) / 100,
                "oi": int(msg.get("open_interest", 0)),
                "prev_oi": 0,
                "turnover": 0.0,
                "bid_price": float(best_bid.get("price", 0)) / 100,
                "bid_qty": int(best_bid.get("quantity", 0)),
                "ask_price": float(best_ask.get("price", 0)) / 100,
                "ask_qty": int(best_ask.get("quantity", 0)),
            }
        except Exception as e:
            logger.debug(f"Failed to parse AngelOne tick: {e} | {str(msg)[:150]}")
            return None

    def ws_stop_streaming(self):
        self._streaming = False
        self._callbacks.clear()
        logger.info("AngelOne tick streaming stopped (callbacks cleared).")

    def ws_disconnect(self):
        self._streaming = False
        if self._ws:
            try:
                self._ws.close_connection()
            except Exception:
                pass
        self._ws_connected = False
        self._ws = None
        logger.info("AngelOne WebSocket disconnected.")

    @property
    def is_ws_connected(self) -> bool:
        return self._ws_connected

    # ── Backward-compat aliases (old TrueData code used tcp_*) ────────────────
    def tcp_connect(self) -> bool:
        return self.ws_connect()

    def tcp_subscribe(self, symbols: List[str]):
        return self.ws_subscribe(symbols)

    def tcp_start_streaming(self, callback: Callable):
        return self.ws_start_streaming(callback)

    def tcp_stop_streaming(self):
        return self.ws_stop_streaming()

    def tcp_disconnect(self):
        return self.ws_disconnect()

    @property
    def is_tcp_connected(self) -> bool:
        return self._ws_connected

    # ── Convenience ────────────────────────────────────────────────────────

    def fetch_all_historical(
        self, symbols: Optional[List[str]] = None, bar_days: int = 180, tick_days: int = 5,
    ) -> dict:
        """Fetch minute bars for all given symbols. Ticks are always empty (see gap above)."""
        from config.settings import SYMBOLS, ANGEL_INDEX_FUTURES_SYMBOLS

        symbols = symbols or [ANGEL_INDEX_FUTURES_SYMBOLS.get(s, s) for s in SYMBOLS]
        all_minutes = [df for s in symbols if not (df := self.fetch_historical_minute_bars(s, days=bar_days)).empty]

        return {
            "minute_bars": pd.concat(all_minutes, ignore_index=True) if all_minutes else pd.DataFrame(),
            "ticks": pd.DataFrame(),
        }

    def disconnect(self):
        self.ws_disconnect()
        self._jwt_token = None
        self._authenticated = False
        logger.info("AngelOne fully disconnected.")
