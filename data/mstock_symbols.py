"""
mStock Symbol Resolution
─────────────────────────
Resolves plain symbol names to the numeric instrument/security token
mStock's `get_historical_chart()` needs (order placement does NOT need
this — mStock's place_order takes tradingsymbol + exchange directly, see
broker/mstock_adapter.py's docstring).

Unlike AngelOne's anonymous, no-auth instrument-master JSON, mStock's
`get_instruments()` requires an authenticated MConnect session — so this
resolver takes a connected MStockAdapter (or raw MConnect) rather than
downloading the master itself on import. Cached to disk with the same TTL
convention as data/angelone_symbols.py so a restart doesn't force a
re-fetch (and re-auth) on every call.

Response field names (instrument_token, tradingsymbol, exchange, expiry,
strike, lot_size, instrument_type, name) are inferred from mStock's close
parity with Kite Connect's own instrument-dump conventions — the SDK
source only confirms the response is JSON, not its exact schema (see
broker/mstock_adapter.py's docstring for the same caveat). Verify the
actual keys the first time this runs against a live account (log a sample
row) and adjust _index()/lookups below if they differ.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from config.settings import BASE_DIR
from utils.logger import get_logger

logger = get_logger("mstock_symbols")

_CACHE_PATH = BASE_DIR / "data" / "jugaad_cache" / "mstock_instrument_master.json"
_CACHE_TTL = timedelta(hours=20)  # master is republished daily, mirrors AngelOne's TTL


class MStockSymbolResolver:
    """Loads and queries mStock's instrument master."""

    def __init__(self):
        self._rows: list[dict] = []
        self._by_symbol: dict[str, dict] = {}  # "NFO:NIFTY26041323800PE" -> row
        self._loaded_at: Optional[datetime] = None

    def load(self, mconnect=None, force: bool = False) -> bool:
        """
        Download (or read cached) instrument master and index it.

        `mconnect` is a connected tradingapi_a.mconnect.MConnect instance
        (e.g. MStockAdapter()._mc after authenticate() succeeds) — required
        only on a cache miss, since get_instruments() needs an authenticated
        session unlike AngelOne's public dump.
        """
        if not force and self._rows:
            return True

        cached = self._read_cache()
        if cached is not None and not force:
            self._index(cached)
            return True

        if mconnect is None:
            logger.error("mStock instrument master not cached and no authenticated MConnect given")
            return False

        try:
            rows = self._parse_instruments_response(mconnect.get_instruments())
            if not rows:
                raise ValueError("empty instrument list in response")
            self._write_cache(rows)
            self._index(rows)
            logger.info(f"mStock instrument master loaded: {len(rows)} rows")
            return True
        except Exception as e:
            logger.error(f"Failed to fetch mStock instrument master: {e}")
            stale = self._read_cache(ignore_ttl=True)
            if stale is not None:
                logger.warning("Using stale cached mStock instrument master")
                self._index(stale)
                return True
            return False

    def _parse_instruments_response(self, resp) -> list[dict]:
        """
        get_instruments() returns raw CSV bytes, not JSON — confirmed live:
        the original JSON-only parsing crashed with "'bytes' object has no
        attribute 'get'" the first time this ran against a real account.
        Kite Connect's own instrument dump (mStock Type A mirrors it) is a
        CSV with a header row (instrument_token, exchange_token,
        tradingsymbol, name, last_price, expiry, strike, tick_size,
        lot_size, instrument_type, segment, exchange) — parse defensively:
        try JSON first (in case a future SDK version changes this), fall
        back to CSV bytes/text.
        """
        import csv
        import io

        if isinstance(resp, dict):
            return resp.get("data") or []

        raw = resp
        if hasattr(raw, "json") and hasattr(raw, "content"):
            # requests.Response: try JSON, but don't let a JSONDecodeError
            # here mask the real content — fall through to CSV on failure.
            try:
                parsed = raw.json()
                if isinstance(parsed, dict):
                    return parsed.get("data") or []
                if isinstance(parsed, list):
                    return parsed
            except ValueError:
                pass
            raw = raw.content

        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if isinstance(raw, str):
            reader = csv.DictReader(io.StringIO(raw))
            return list(reader)

        return []

    def _read_cache(self, ignore_ttl: bool = False) -> Optional[list]:
        if not _CACHE_PATH.exists():
            return None
        if not ignore_ttl:
            age = datetime.now() - datetime.fromtimestamp(_CACHE_PATH.stat().st_mtime)
            if age > _CACHE_TTL:
                return None
        try:
            with open(_CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

    def _write_cache(self, rows: list):
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(rows, f)

    def _index(self, rows: list):
        self._rows = rows
        self._by_symbol = {
            f"{r.get('exchange')}:{r.get('tradingsymbol')}": r for r in rows
        }
        self._loaded_at = datetime.now()

    # ── Lookups ──────────────────────────────────────────────────────────────

    def token_for(self, symbol: str, exchange: str = "NFO", mconnect=None) -> Optional[dict]:
        """
        Return the instrument row (containing instrument_token) for an
        exact tradingsymbol, e.g. token_for("NIFTY 50", "NSE") or
        token_for("NIFTY26041323800PE", "NFO").
        """
        self.load(mconnect=mconnect)
        return self._by_symbol.get(f"{exchange}:{symbol}")

    def current_futures_symbol(self, underlying: str, mconnect=None) -> Optional[dict]:
        """
        Nearest-expiry FUT row for an index underlying — mirrors
        AngelSymbolResolver.current_futures_symbol(). `instrument_type` value
        ("FUT") and `expiry` format ("YYYY-MM-DD") follow Kite Connect's own
        instrument-dump convention, not AngelOne's ("FUTIDX" / "DDMMMYYYY")
        — unverified against a live mStock response, see module docstring.
        """
        self.load(mconnect=mconnect)
        candidates = [
            r for r in self._rows
            if r.get("name") == underlying
            and r.get("instrument_type") == "FUT"
            and r.get("exchange") == "NFO"
        ]
        if not candidates:
            return None

        def _expiry(r):
            try:
                return datetime.strptime(r["expiry"], "%Y-%m-%d")
            except (KeyError, ValueError, TypeError):
                return datetime.max

        candidates.sort(key=_expiry)
        return candidates[0]

    def option_symbol_for(
        self, underlying: str, expiry, strike: float, opt_type: str,
        exchange: str = "NFO", mconnect=None,
    ) -> Optional[dict]:
        """
        Resolve an option contract by underlying/expiry/strike/CE|PE rather
        than guessing mStock's tradingsymbol string — mirrors
        AngelSymbolResolver.option_symbol_for(). Kite Connect's own
        instrument dumps use "CE"/"PE" directly as `instrument_type` (unlike
        AngelOne's "OPTIDX" + a symbol suffix) and store `expiry` as
        "YYYY-MM-DD" with `strike` as the real price (not paise-scaled) —
        unverified against a live mStock response, see module docstring.
        """
        self.load(mconnect=mconnect)
        expiry_str = expiry.strftime("%Y-%m-%d")
        opt_type = opt_type.upper()
        for r in self._rows:
            if (r.get("name") == underlying and r.get("instrument_type") == opt_type
                    and r.get("expiry") == expiry_str and r.get("exchange") == exchange):
                try:
                    if abs(float(r.get("strike", -1)) - float(strike)) < 0.01:
                        return r
                except (TypeError, ValueError):
                    continue
        return None


# Module-level singleton — mirrors data/angelone_symbols.py's resolver pattern.
resolver = MStockSymbolResolver()
