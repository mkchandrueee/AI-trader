"""
mStock Trading API Adapter (Type A)
────────────────────────────────────
Real-money execution via Mirae Asset's mStock Trading API — the broker used
going forward for live order execution, once TRADE_MODE leaves "paper".

Authentication mirrors AngelOne's shape: user ID + password live in `.env`,
and the TOTP *secret* (not a one-time code) computes the live 6-digit code
via `pyotp` at call time — no OTP-by-SMS step, no browser/OAuth redirect.

Required env vars:
  MSTOCK_API_KEY      — from an app registered at https://trade.mstock.com
  MSTOCK_USER_ID       — your mStock client/user ID
  MSTOCK_PASSWORD      — your mStock trading password
  MSTOCK_TOTP_SECRET   — base32 secret from enabling TOTP on trade.mstock.com
                         (Hamburger Menu -> Key Products -> Trading APIs ->
                         Enable TOTP), NOT a 6-digit code

Never run authentication yourself from chat — see
scripts/mstock_check_auth.py, which the user runs locally so the actual
secret values never appear in any tool-call transcript.

Install:
  pip install mStock-TradingApi-A

Docs: https://tradingapi.mstock.com/docs/v1/typeA/User/
API shape confirmed against the official SDK source
(github.com/MiraeAsset-mStock/pytradingapi-typeA/blob/main/tradingapi_a/mconnect.py):
place_order takes tradingsymbol + exchange directly (like Kite Connect's
real API) — no instrument-token lookup needed for orders, unlike AngelOne.
A token IS needed for historical-candle calls (get_historical_chart); that
lookup lives in data/mstock_symbols.py, used by the market-data path, not
this order-execution adapter.

Request-payload field names for place_order/modify_order/cancel_order/
get_net_position/get_order_book/verify_totp are taken directly from the SDK
source, not guessed. Field names *inside* a response body (e.g. the exact
keys in one get_net_position row) were not visible in the source and are
inferred from mStock's close parity with Kite Connect's conventions
elsewhere in this API — this has not been exercised against a live mStock
account (that requires real credentials, which this codebase never
handles). Verify against the real response the first time this runs live
and adjust _parse_order()/get_positions() below if the keys differ.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import pyotp

from broker.base_adapter import (
    BrokerAdapter,
    OrderRequest,
    OrderResponse,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    ProductType,
)
from config.settings import (
    MSTOCK_API_KEY,
    MSTOCK_USER_ID,
    MSTOCK_PASSWORD,
    MSTOCK_TOTP_SECRET,
)
from utils.logger import get_logger

logger = get_logger("mstock_adapter")


def _as_json(resp) -> dict:
    """
    Every MConnect call (login, verify_totp, place_order, get_net_position,
    get_order_book, get_fund_summary, ...) returns the RAW `requests.Response`
    object from its internal `_get()`/`_post()` — not already-parsed JSON,
    despite what the API docs summary implies. Confirmed live: calling
    `.get("data")` directly on it raised `'Response' object has no attribute
    'get'`. Normalize once here instead of a `.json()` call at every site.
    """
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

# Map our OrderType -> mStock ordertype string (Kite-Connect-shaped values)
_ORDER_TYPE_MAP = {
    OrderType.MARKET: "MARKET",
    OrderType.LIMIT: "LIMIT",
    OrderType.SL: "SL",
    OrderType.SL_MARKET: "SL-M",
}

# Map our ProductType -> mStock producttype string
_PRODUCT_MAP = {
    ProductType.MIS: "MIS",
    ProductType.NRML: "NRML",
    ProductType.CNC: "CNC",
}

_STATUS_MAP = {
    "complete": OrderStatus.COMPLETE,
    "rejected": OrderStatus.REJECTED,
    "cancelled": OrderStatus.CANCELLED,
    "open": OrderStatus.OPEN,
    "trigger pending": OrderStatus.OPEN,
    "open pending": OrderStatus.PENDING,
    "pending": OrderStatus.PENDING,
    "validation pending": OrderStatus.PENDING,
    "put order req received": OrderStatus.PENDING,
}


class MStockAdapter(BrokerAdapter):
    """
    mStock Trading API (Type A) broker adapter for real-money trading.

    Usage:
        adapter = MStockAdapter()
        if adapter.authenticate():
            resp = adapter.buy("NIFTY26041323800PE", quantity=65, tag="bearish_momentum")
    """

    def __init__(self):
        self._mc = None  # tradingapi_a.mconnect.MConnect instance
        self._connected = False
        self._user_id: str = ""
        # mStock's own rejection reason, or a local/network error — never a
        # credential value, safe to show in the UI and safe to log.
        self._last_error: str = ""

    # ── Authentication ────────────────────────────────────────────────

    def authenticate(self) -> bool:
        """
        Non-interactive path for background/startup connects: reads user ID,
        password, and the TOTP *secret* from .env and computes the live
        6-digit code via pyotp. Used by OrderManager.connect() at Flask
        startup.
        """
        if not (MSTOCK_API_KEY and MSTOCK_USER_ID and MSTOCK_PASSWORD and MSTOCK_TOTP_SECRET):
            self._last_error = "mStock credentials not fully set in .env"
            logger.error(self._last_error)
            return False

        totp_code = pyotp.TOTP(MSTOCK_TOTP_SECRET).now()
        return self._login(MSTOCK_USER_ID, MSTOCK_PASSWORD, totp_code)

    def connect_manual(self, user_id: str, password: str, totp_code: str) -> bool:
        """
        Interactive path for the dashboard's "Connect" action: user ID,
        password, and the current 6-digit TOTP code (read off the user's
        own authenticator app) are typed into the browser and sent straight
        to this Flask backend — never through any chat session. Doesn't
        require MSTOCK_TOTP_SECRET to be stored anywhere.
        """
        if not MSTOCK_API_KEY:
            self._last_error = "MSTOCK_API_KEY is not set in .env (register an app at https://trade.mstock.com)"
            logger.error(self._last_error)
            return False
        if not (user_id and password and totp_code):
            self._last_error = "User ID, password, and TOTP code are all required"
            logger.error(self._last_error)
            return False
        return self._login(user_id, password, totp_code)

    def _login(self, user_id: str, password: str, totp_code: str) -> bool:
        try:
            from tradingapi_a.mconnect import MConnect
        except ImportError as e:
            # Show the real cause: pip having installed the package doesn't
            # mean every import path works — mticker.py's Twisted/TLS stack
            # (pyOpenSSL, service_identity) isn't declared as a dependency
            # and failed with a *different* ModuleNotFoundError the first
            # time this ran live. A hardcoded "not installed" message here
            # would have hidden that entirely.
            self._last_error = f"mStock-TradingApi-A import failed: {e}. Run: pip install mStock-TradingApi-A"
            logger.error(self._last_error)
            return False

        try:
            mc = MConnect()
            mc.set_api_key(MSTOCK_API_KEY)
            mc.login(user_id, password)

            session = _as_json(mc.verify_totp(MSTOCK_API_KEY, totp_code))
            data = session.get("data") or {}
            access_token = data.get("access_token")

            if not access_token:
                self._last_error = session.get("message") or session.get("error_message") or "mStock rejected the login (no reason given)"
                logger.error(f"mStock login failed: {self._last_error}")
                self._connected = False
                return False

            mc.set_access_token(access_token)
            self._mc = mc
            self._connected = True
            self._user_id = user_id
            self._last_error = ""
            logger.info(f"mStock authenticated: {user_id}")
            return True

        except Exception as e:
            # str(e) here is a network/library-level failure (timeout, DNS,
            # malformed response) or mStock's own exception message — still
            # safe to show, it never includes the credentials passed in.
            self._last_error = str(e)
            logger.error(f"mStock authentication failed: {e}")
            self._connected = False
            return False

    @property
    def last_error(self) -> str:
        """mStock's own rejection reason from the most recent failed login attempt, if any."""
        return self._last_error

    def disconnect(self):
        """Drop the current session — dashboard 'Disconnect' action."""
        if self._mc is not None and self._connected:
            try:
                self._mc.logout()
            except Exception as e:
                logger.debug(f"mStock logout failed (ignoring): {e}")
        self._mc = None
        self._connected = False
        self._user_id = ""

    @property
    def is_connected(self) -> bool:
        return self._connected and self._mc is not None

    @property
    def user_id(self) -> str:
        return self._user_id or MSTOCK_USER_ID

    @property
    def broker_name(self) -> str:
        return "mStock"

    # ── Order Placement ───────────────────────────────────────────────

    def place_order(self, request: OrderRequest) -> OrderResponse:
        if not self.is_connected:
            return OrderResponse(status=OrderStatus.ERROR, message="Not connected to mStock")

        try:
            resp = self._mc.place_order(
                "regular",                                          # _variety
                request.symbol,                                     # _tradingsymbol
                request.exchange,                                   # _exchange
                request.side.value,                                 # _transaction_type
                _ORDER_TYPE_MAP.get(request.order_type, "MARKET"),  # _order_type
                str(request.quantity),                              # _quantity
                _PRODUCT_MAP.get(request.product, "MIS"),           # _product
                "DAY",                                              # _validity
                str(request.price) if request.order_type in (OrderType.LIMIT, OrderType.SL) else "0",
                str(request.trigger_price) if request.order_type in (OrderType.SL, OrderType.SL_MARKET) else "0",
                "0",                                                # _disclosed_quantity
                request.tag[:20] if request.tag else "",            # _tag
            )
            resp = _as_json(resp)
            data = resp.get("data") or {}
            order_id = data.get("order_id") or data.get("orderid")

            if not order_id:
                message = resp.get("message") or "mStock did not return an order ID"
                logger.error(f"mStock order FAILED: {request.symbol} {request.side.value} — {message}")
                return OrderResponse(status=OrderStatus.REJECTED, message=message, timestamp=datetime.now())

            logger.info(
                f"mStock ORDER: {request.side.value} {request.symbol} "
                f"x{request.quantity} [{request.order_type.value}] -> {order_id}"
            )
            return OrderResponse(
                order_id=str(order_id),
                status=OrderStatus.OPEN,
                filled_quantity=0,
                message=f"Order placed: {order_id}",
                timestamp=datetime.now(),
                raw=data,
            )

        except Exception as e:
            error_msg = str(e)
            logger.error(f"mStock order FAILED: {request.symbol} {request.side.value} — {error_msg}")
            status = OrderStatus.ERROR if ("network" in error_msg.lower() or "timeout" in error_msg.lower()) else OrderStatus.REJECTED
            return OrderResponse(status=status, message=error_msg, timestamp=datetime.now())

    def modify_order(
        self,
        order_id: str,
        quantity: Optional[int] = None,
        price: Optional[float] = None,
        trigger_price: Optional[float] = None,
        order_type: Optional[OrderType] = None,
    ) -> OrderResponse:
        if not self.is_connected:
            return OrderResponse(status=OrderStatus.ERROR, message="Not connected")

        try:
            self._mc.modify_order(
                order_id,
                _ORDER_TYPE_MAP.get(order_type, "MARKET") if order_type is not None else "",
                str(quantity) if quantity is not None else "",
                str(price) if price is not None else "",
                "DAY",
                str(trigger_price) if trigger_price is not None else "",
                "0",
            )
            logger.info(f"mStock MODIFY: {order_id} trigger=Rs.{trigger_price}")
            return OrderResponse(order_id=order_id, status=OrderStatus.OPEN, message="Modified")
        except Exception as e:
            logger.error(f"mStock MODIFY failed: {order_id} — {e}")
            return OrderResponse(order_id=order_id, status=OrderStatus.ERROR, message=str(e))

    def cancel_order(self, order_id: str) -> OrderResponse:
        if not self.is_connected:
            return OrderResponse(status=OrderStatus.ERROR, message="Not connected")

        try:
            self._mc.cancel_order(order_id)
            logger.info(f"mStock CANCEL: {order_id}")
            return OrderResponse(order_id=order_id, status=OrderStatus.CANCELLED, message="Cancelled")
        except Exception as e:
            logger.error(f"mStock CANCEL failed: {order_id} — {e}")
            return OrderResponse(order_id=order_id, status=OrderStatus.ERROR, message=str(e))

    # ── Position & Order Queries ──────────────────────────────────────

    def get_positions(self) -> list[Position]:
        if not self.is_connected:
            return []

        try:
            resp = _as_json(self._mc.get_net_position())
            positions = []
            for pos in (resp.get("data") or []):
                netqty = int(pos.get("net_quantity", pos.get("netqty", 0)) or 0)
                if netqty != 0:
                    positions.append(Position(
                        symbol=pos.get("tradingsymbol", ""),
                        exchange=pos.get("exchange", "NFO"),
                        quantity=netqty,
                        average_price=float(pos.get("average_price", pos.get("avgnetprice", 0)) or 0),
                        last_price=float(pos.get("last_price", pos.get("ltp", 0)) or 0),
                        pnl=float(pos.get("pnl", pos.get("unrealised", 0)) or 0),
                        product=ProductType.MIS if pos.get("product") == "MIS" else ProductType.NRML,
                    ))
            return positions
        except Exception as e:
            logger.error(f"mStock get_positions failed: {e}")
            return []

    def get_order_status(self, order_id: str) -> OrderResponse:
        if not self.is_connected:
            return OrderResponse(status=OrderStatus.ERROR, message="Not connected")

        try:
            resp = _as_json(self._mc.get_order_book())
            for o in (resp.get("data") or []):
                if str(o.get("order_id", o.get("orderid"))) == str(order_id):
                    return self._parse_order(o)
            return OrderResponse(order_id=order_id, status=OrderStatus.ERROR, message="No history")
        except Exception as e:
            logger.error(f"mStock order_status failed: {order_id} — {e}")
            return OrderResponse(order_id=order_id, status=OrderStatus.ERROR, message=str(e))

    def get_orders_today(self) -> list[OrderResponse]:
        if not self.is_connected:
            return []

        try:
            resp = _as_json(self._mc.get_order_book())
            return [self._parse_order(o) for o in (resp.get("data") or [])]
        except Exception as e:
            logger.error(f"mStock get_orders failed: {e}")
            return []

    def _parse_order(self, o: dict) -> OrderResponse:
        return OrderResponse(
            order_id=str(o.get("order_id", o.get("orderid", ""))),
            status=_STATUS_MAP.get(str(o.get("status", o.get("order_status", ""))).lower(), OrderStatus.PENDING),
            filled_quantity=int(o.get("filled_quantity", o.get("filledshares", 0)) or 0),
            average_price=float(o.get("average_price", o.get("averageprice", 0)) or 0),
            message=o.get("status_message", o.get("text", "")),
            raw=o,
        )

    # ── Safety ────────────────────────────────────────────────────────

    def kill_switch(self) -> list[OrderResponse]:
        """
        EMERGENCY: Cancel all open orders and close all positions at market.
        Runs even if some individual operations fail — logs and continues.
        """
        if not self.is_connected:
            logger.error("KILL SWITCH: not connected to mStock!")
            return []

        responses = []

        try:
            book = _as_json(self._mc.get_order_book())
            for o in (book.get("data") or []):
                status = str(o.get("status", o.get("order_status", ""))).lower()
                if status in ("open", "pending", "open pending", "trigger pending", "validation pending"):
                    try:
                        self._mc.cancel_order(o.get("order_id", o.get("orderid")))
                        logger.warning(f"KILL: cancelled order {o.get('order_id', o.get('orderid'))}")
                    except Exception as e:
                        logger.error(f"KILL: cancel {o.get('order_id')} failed: {e}")
        except Exception as e:
            logger.error(f"KILL: fetch orders failed: {e}")

        try:
            positions = self.get_positions()
            for pos in positions:
                if pos.quantity > 0:
                    resp = self.sell(pos.symbol, pos.quantity, order_type=OrderType.MARKET, tag="KILL_SWITCH")
                    responses.append(resp)
                    if resp.status == OrderStatus.ERROR:
                        logger.warning(f"KILL: retrying {pos.symbol}")
                        responses.append(self.sell(pos.symbol, pos.quantity, tag="KILL_RETRY"))
                elif pos.quantity < 0:
                    resp = self.buy(pos.symbol, abs(pos.quantity), order_type=OrderType.MARKET, tag="KILL_SWITCH")
                    responses.append(resp)
        except Exception as e:
            logger.error(f"KILL: close positions failed: {e}")

        logger.warning(f"KILL SWITCH COMPLETE: {len(responses)} exit orders placed")
        return responses

    # ── Helpers ────────────────────────────────────────────────────────

    def get_margins(self) -> dict:
        """Return available funds/margin."""
        if not self.is_connected:
            return {}
        try:
            return _as_json(self._mc.get_fund_summary())
        except Exception as e:
            logger.error(f"Margins fetch failed: {e}")
            return {}
