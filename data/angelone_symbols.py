"""
AngelOne Symbol Resolution
──────────────────────────
Resolves plain symbol names (NIFTY, NIFTY-FUT, NIFTY26041323800PE) to the
numeric `symboltoken` + exchange segment AngelOne's SmartAPI needs for every
call (getCandleData, websocket subscribe, placeOrder).

Source: AngelOne's public instrument master — a free, no-auth JSON dump
published daily, refreshed once per process and cached to disk so we don't
re-download ~150k rows on every import.

  GET https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json

Each row looks like:
  {"token":"26000","symbol":"NIFTY","name":"NIFTY","expiry":"","strike":"-1.000000",
   "lotsize":"1","instrumenttype":"","exch_seg":"NSE","tick_size":"0.05"}
  {"token":"35863","symbol":"NIFTY28MAR24FUT","name":"NIFTY","expiry":"28MAR2024",
   "strike":"-1.000000","lotsize":"75","instrumenttype":"FUTIDX","exch_seg":"NFO", ...}
  {"token":"46569","symbol":"NIFTY07MAR2422500CE","name":"NIFTY","expiry":"07MAR2024",
   "strike":"2250000.000000","lotsize":"75","instrumenttype":"OPTIDX","exch_seg":"NFO", ...}
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests

from config.settings import ANGEL_INSTRUMENT_MASTER_URL, BASE_DIR
from utils.logger import get_logger

logger = get_logger("angelone_symbols")

_CACHE_PATH = BASE_DIR / "data" / "jugaad_cache" / "angel_instrument_master.json"
_CACHE_TTL = timedelta(hours=20)  # master is republished daily


class AngelSymbolResolver:
    """Loads and queries the AngelOne instrument master."""

    def __init__(self):
        self._rows: list[dict] = []
        self._by_symbol: dict[str, dict] = {}  # "NSE:NIFTY 50" -> row
        self._loaded_at: Optional[datetime] = None

    def load(self, force: bool = False) -> bool:
        """Download (or read cached) instrument master and index it."""
        if not force and self._rows:
            return True

        cached = self._read_cache()
        if cached is not None and not force:
            self._index(cached)
            return True

        try:
            resp = requests.get(ANGEL_INSTRUMENT_MASTER_URL, timeout=60)
            resp.raise_for_status()
            rows = resp.json()
            self._write_cache(rows)
            self._index(rows)
            logger.info(f"AngelOne instrument master loaded: {len(rows)} rows")
            return True
        except requests.RequestException as e:
            logger.error(f"Failed to fetch AngelOne instrument master: {e}")
            # Fall back to a stale cache rather than failing outright
            stale = self._read_cache(ignore_ttl=True)
            if stale is not None:
                logger.warning("Using stale cached instrument master")
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
        self._by_symbol = {f"{r.get('exch_seg')}:{r.get('symbol')}": r for r in rows}
        self._loaded_at = datetime.now()

    # ── Lookups ──────────────────────────────────────────────────────────────

    def token_for(self, symbol: str, exchange: str = "NFO") -> Optional[dict]:
        """
        Return {"token": "...", "symbol": "...", "exch_seg": "..."} for an
        exact tradingsymbol, e.g. token_for("NIFTY 50", "NSE") or
        token_for("NIFTY26041323800PE", "NFO").
        """
        self.load()
        return self._by_symbol.get(f"{exchange}:{symbol}")

    def option_symbol_for(self, underlying: str, expiry, strike: float, opt_type: str) -> Optional[dict]:
        """
        Resolve an option contract by its structured fields rather than
        guessing a tradingsymbol string. AngelOne's real tradingsymbol for
        options is "{underlying}{DD}{MMM}{YY}{strike}{CE|PE}" (e.g.
        "NIFTY08SEP2622100PE") — NOT the "{underlying}{yymmdd}{strike}{type}"
        format used elsewhere in this project as an internal/DB symbol alias
        (see backtest/option_resolver.py's build_option_symbol and
        CLAUDE.md's documented DB convention). token_for() with that
        DB-style string never matches a real row; this looks up by the
        instrument master's own expiry/strike/instrumenttype fields instead,
        so the caller doesn't need to know AngelOne's exact string format.

        `expiry` is a date; `strike` is the actual strike price (e.g. 22100,
        not AngelOne's internal ×100 integer storage — that scaling is
        handled here).
        """
        self.load()
        expiry_str = expiry.strftime("%d%b%Y").upper()
        target_strike = round(float(strike) * 100)
        opt_type = opt_type.upper()
        for r in self._rows:
            if (r.get("name") == underlying and r.get("instrumenttype") == "OPTIDX"
                    and r.get("expiry") == expiry_str and r.get("exch_seg") == "NFO"
                    and str(r.get("symbol", "")).endswith(opt_type)):
                try:
                    if abs(float(r.get("strike", -1)) - target_strike) < 1:
                        return r
                except (TypeError, ValueError):
                    continue
        return None

    def current_futures_symbol(self, underlying: str) -> Optional[dict]:
        """
        Return the nearest-expiry FUTIDX row for an index underlying
        (NIFTY/BANKNIFTY/FINNIFTY) — AngelOne's equivalent of TrueData's
        synthetic "NIFTY-I" continuous-futures symbol.
        """
        self.load()
        candidates = [
            r for r in self._rows
            if r.get("name") == underlying
            and r.get("instrumenttype") == "FUTIDX"
            and r.get("exch_seg") == "NFO"
        ]
        if not candidates:
            return None

        def _expiry(r):
            try:
                return datetime.strptime(r["expiry"], "%d%b%Y")
            except (KeyError, ValueError):
                return datetime.max

        candidates.sort(key=_expiry)
        return candidates[0]

    def option_symbol(
        self, underlying: str, expiry_ddmmmyyyy: str, strike: float, option_type: str
    ) -> Optional[dict]:
        """
        Resolve an OPTIDX row by underlying + expiry ("07MAR2024") + strike +
        CE/PE. `strike` is the real strike price (e.g. 22500), not the
        paise-scaled value AngelOne stores internally.
        """
        self.load()
        strike_scaled = f"{strike * 100:.6f}"
        for r in self._rows:
            if (
                r.get("name") == underlying
                and r.get("instrumenttype") == "OPTIDX"
                and r.get("expiry") == expiry_ddmmmyyyy
                and r.get("strike") == strike_scaled
                and r.get("symbol", "").endswith(option_type)
            ):
                return r
        return None

    def expiries_for(self, underlying: str, instrument_type: str = "OPTIDX") -> list[str]:
        """Return sorted upcoming expiry date strings ('07MAR2024') for an underlying."""
        self.load()
        expiries = {
            r["expiry"]
            for r in self._rows
            if r.get("name") == underlying
            and r.get("instrumenttype") == instrument_type
            and r.get("expiry")
        }

        def _key(e):
            try:
                return datetime.strptime(e, "%d%b%Y")
            except ValueError:
                return datetime.max

        return sorted(expiries, key=_key)


# Module-level singleton — the instrument master is ~150k rows, load once.
resolver = AngelSymbolResolver()
