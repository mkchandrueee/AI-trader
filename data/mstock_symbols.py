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
            resp = mconnect.get_instruments()
            # MConnect calls return the raw requests.Response object, not
            # already-parsed JSON — confirmed live via broker/mstock_adapter.py's
            # _as_json() (same SDK quirk, hit there first: calling .get()
            # directly on the response raised "'Response' object has no
            # attribute 'get'").
            if hasattr(resp, "json"):
                resp = resp.json()
            rows = (resp or {}).get("data") or resp or []
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


# Module-level singleton — mirrors data/angelone_symbols.py's resolver pattern.
resolver = MStockSymbolResolver()
