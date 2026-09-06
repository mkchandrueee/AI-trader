"""
AngelOne SmartAPI Adapter
──────────────────────────
Real-money execution via AngelOne's SmartAPI. Replaces the Zerodha Kite
Connect adapter — free for retail accounts (no separate API subscription).

Authentication is fully non-interactive: client code + password/PIN live in
`.env`, and the TOTP *secret* (not a one-time code) is used to compute the
live 6-digit code via `pyotp` at call time. No OAuth redirect, no manual
login-URL step.

Required env vars:
  ANGEL_API_KEY          — from https://smartapi.angelbroking.com (create an app)
  ANGEL_CLIENT_CODE      — your AngelOne client ID
  ANGEL_PASSWORD_OR_PIN  — your AngelOne trading PIN
  ANGEL_TOTP_SECRET      — base32 secret from enabling TOTP in the AngelOne app
                           (Profile → Settings → Enable TOTP), NOT a 6-digit code

Never run authentication yourself from chat — see
scripts/angelone_check_auth.py, which the user runs locally so the actual
secret values never appear in any tool-call transcript.

Install:
  pip install smartapi-python pyotp

Docs: https://smartapi.angelbroking.com/docs
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
    ANGEL_API_KEY,
    ANGEL_CLIENT_CODE,
    ANGEL_PASSWORD_OR_PIN,
    ANGEL_TOTP_SECRET,
)
from data.angelone_symbols import resolver as symbol_resolver
from utils.logger import get_logger

logger = get_logger("angelone_adapter")

# Map our OrderType → AngelOne ordertype string
_ORDER_TYPE_MAP = {
    OrderType.MARKET: "MARKET",
    OrderType.LIMIT: "LIMIT",
    OrderType.SL: "STOPLOSS_LIMIT",
    OrderType.SL_MARKET: "STOPLOSS_MARKET",
}

# Map our ProductType → AngelOne producttype string
_PRODUCT_MAP = {
    ProductType.MIS: "INTRADAY",
    ProductType.NRML: "CARRYFORWARD",
    ProductType.CNC: "DELIVERY",
}

_STATUS_MAP = {
    "complete": OrderStatus.COMPLETE,
    "rejected": OrderStatus.REJECTED,
    "cancelled": OrderStatus.CANCELLED,
    "open": OrderStatus.OPEN,
    "open pending": OrderStatus.PENDING,
    "pending": OrderStatus.PENDING,
    "trigger pending": OrderStatus.OPEN,
}


class AngelOneAdapter(BrokerAdapter):
    """
    AngelOne SmartAPI broker adapter for real-money trading.

    Usage:
        adapter = AngelOneAdapter()
        if adapter.authenticate():
            resp = adapter.buy("NIFTY26041323800PE", quantity=65, tag="bearish_momentum")
    """

    def __init__(self):
        self._smart = None  # SmartApi.SmartConnect instance
        self._connected = False
        self._client_code: str = ""

    # ── Authentication ────────────────────────────────────────────────

    def authenticate(self) -> bool:
        """
        Non-interactive path for background/startup connects: reads
        client code, PIN, and the TOTP *secret* from .env and computes the
        live 6-digit code via pyotp. Used by OrderManager.connect() at
        Flask startup.
        """
        if not (ANGEL_API_KEY and ANGEL_CLIENT_CODE and ANGEL_PASSWORD_OR_PIN and ANGEL_TOTP_SECRET):
            logger.error("AngelOne credentials not fully set in .env")
            return False

        totp_code = pyotp.TOTP(ANGEL_TOTP_SECRET).now()
        return self._login(ANGEL_CLIENT_CODE, ANGEL_PASSWORD_OR_PIN, totp_code)

    def connect_manual(self, client_code: str, pin: str, totp_code: str) -> bool:
        """
        Interactive path for the dashboard's "Connect" action: client code,
        PIN/password, and the current 6-digit TOTP code (read off the
        user's own authenticator app) are typed into the browser and sent
        straight to this Flask backend — never through any chat session.
        Doesn't require ANGEL_TOTP_SECRET to be stored anywhere.
        """
        if not (ANGEL_API_KEY and client_code and pin and totp_code):
            logger.error("AngelOne connect: client code, PIN, and TOTP code are all required "
                         "(ANGEL_API_KEY must also be set in .env)")
            return False
        return self._login(client_code, pin, totp_code)

    def _login(self, client_code: str, pin: str, totp_code: str) -> bool:
        try:
            from SmartApi import SmartConnect
        except ImportError:
            logger.error("smartapi-python package not installed. Run: pip install smartapi-python")
            return False

        try:
            self._smart = SmartConnect(api_key=ANGEL_API_KEY)
            session = self._smart.generateSession(client_code, pin, totp_code)

            if not session.get("status"):
                logger.error(f"AngelOne login failed: {session.get('message')}")
                self._connected = False
                return False

            self._connected = True
            self._client_code = client_code
            logger.info(f"AngelOne authenticated: {client_code}")
            return True

        except Exception as e:
            logger.error(f"AngelOne authentication failed: {e}")
            self._connected = False
            return False

    def disconnect(self):
        """Drop the current session — dashboard 'Disconnect' action."""
        if self._smart is not None and self._connected:
            try:
                self._smart.terminateSession(self._client_code or ANGEL_CLIENT_CODE)
            except Exception as e:
                logger.debug(f"AngelOne terminateSession failed (ignoring): {e}")
        self._smart = None
        self._connected = False
        self._client_code = ""

    @property
    def is_connected(self) -> bool:
        return self._connected and self._smart is not None

    @property
    def client_code(self) -> str:
        return self._client_code or ANGEL_CLIENT_CODE

    @property
    def broker_name(self) -> str:
        return "AngelOne"

    # ── Order Placement ───────────────────────────────────────────────

    def place_order(self, request: OrderRequest) -> OrderResponse:
        if not self.is_connected:
            return OrderResponse(status=OrderStatus.ERROR, message="Not connected to AngelOne")

        row = symbol_resolver.token_for(request.symbol, request.exchange)
        if row is None:
            return OrderResponse(status=OrderStatus.ERROR,
                                  message=f"Could not resolve symboltoken for {request.symbol}")

        try:
            params = {
                "variety": "NORMAL" if request.order_type != OrderType.SL else "STOPLOSS",
                "tradingsymbol": request.symbol,
                "symboltoken": row["token"],
                "transactiontype": request.side.value,
                "exchange": request.exchange,
                "ordertype": _ORDER_TYPE_MAP.get(request.order_type, "MARKET"),
                "producttype": _PRODUCT_MAP.get(request.product, "INTRADAY"),
                "duration": "DAY",
                "price": str(request.price) if request.order_type in (OrderType.LIMIT, OrderType.SL) else "0",
                "triggerprice": str(request.trigger_price) if request.order_type in (OrderType.SL, OrderType.SL_MARKET) else "0",
                "squareoff": "0",
                "stoploss": "0",
                "quantity": str(request.quantity),
                "ordertag": request.tag[:20] if request.tag else "",
            }

            order_id = self._smart.placeOrder(params)

            logger.info(
                f"AngelOne ORDER: {request.side.value} {request.symbol} "
                f"×{request.quantity} [{request.order_type.value}] → {order_id}"
            )

            return OrderResponse(
                order_id=str(order_id),
                status=OrderStatus.OPEN,
                filled_quantity=0,
                message=f"Order placed: {order_id}",
                timestamp=datetime.now(),
                raw=params,
            )

        except Exception as e:
            error_msg = str(e)
            logger.error(f"AngelOne order FAILED: {request.symbol} {request.side.value} — {error_msg}")

            status = OrderStatus.REJECTED
            if "insufficient" in error_msg.lower():
                status = OrderStatus.REJECTED
            elif "network" in error_msg.lower() or "timeout" in error_msg.lower():
                status = OrderStatus.ERROR

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
            params = {"variety": "NORMAL", "orderid": order_id}
            if quantity is not None:
                params["quantity"] = str(quantity)
            if price is not None:
                params["price"] = str(price)
            if trigger_price is not None:
                params["triggerprice"] = str(trigger_price)
            if order_type is not None:
                params["ordertype"] = _ORDER_TYPE_MAP.get(order_type, "MARKET")

            self._smart.modifyOrder(params)
            logger.info(f"AngelOne MODIFY: {order_id} trigger=₹{trigger_price}")
            return OrderResponse(order_id=order_id, status=OrderStatus.OPEN, message="Modified")

        except Exception as e:
            logger.error(f"AngelOne MODIFY failed: {order_id} — {e}")
            return OrderResponse(order_id=order_id, status=OrderStatus.ERROR, message=str(e))

    def cancel_order(self, order_id: str) -> OrderResponse:
        if not self.is_connected:
            return OrderResponse(status=OrderStatus.ERROR, message="Not connected")

        try:
            self._smart.cancelOrder(order_id, "NORMAL")
            logger.info(f"AngelOne CANCEL: {order_id}")
            return OrderResponse(order_id=order_id, status=OrderStatus.CANCELLED, message="Cancelled")
        except Exception as e:
            logger.error(f"AngelOne CANCEL failed: {order_id} — {e}")
            return OrderResponse(order_id=order_id, status=OrderStatus.ERROR, message=str(e))

    # ── Position & Order Queries ──────────────────────────────────────

    def get_positions(self) -> list[Position]:
        if not self.is_connected:
            return []

        try:
            resp = self._smart.position()
            positions = []
            for pos in (resp.get("data") or []):
                netqty = int(pos.get("netqty", 0))
                if netqty != 0:
                    positions.append(Position(
                        symbol=pos["tradingsymbol"],
                        exchange=pos["exchange"],
                        quantity=netqty,
                        average_price=float(pos.get("avgnetprice", 0)),
                        last_price=float(pos.get("ltp", 0)),
                        pnl=float(pos.get("pnl", 0)),
                        product=ProductType.MIS if pos.get("producttype") == "INTRADAY" else ProductType.NRML,
                    ))
            return positions
        except Exception as e:
            logger.error(f"AngelOne get_positions failed: {e}")
            return []

    def get_order_status(self, order_id: str) -> OrderResponse:
        if not self.is_connected:
            return OrderResponse(status=OrderStatus.ERROR, message="Not connected")

        try:
            resp = self._smart.orderBook()
            for o in (resp.get("data") or []):
                if str(o.get("orderid")) == str(order_id):
                    return OrderResponse(
                        order_id=order_id,
                        status=_STATUS_MAP.get(str(o.get("status", "")).lower(), OrderStatus.PENDING),
                        filled_quantity=int(o.get("filledshares", 0)),
                        average_price=float(o.get("averageprice", 0)),
                        message=o.get("text", ""),
                        raw=o,
                    )
            return OrderResponse(order_id=order_id, status=OrderStatus.ERROR, message="No history")
        except Exception as e:
            logger.error(f"AngelOne order_status failed: {order_id} — {e}")
            return OrderResponse(order_id=order_id, status=OrderStatus.ERROR, message=str(e))

    def get_orders_today(self) -> list[OrderResponse]:
        if not self.is_connected:
            return []

        try:
            resp = self._smart.orderBook()
            return [
                OrderResponse(
                    order_id=str(o.get("orderid")),
                    status=_STATUS_MAP.get(str(o.get("status", "")).lower(), OrderStatus.PENDING),
                    filled_quantity=int(o.get("filledshares", 0)),
                    average_price=float(o.get("averageprice", 0)),
                    message=o.get("text", ""),
                    raw=o,
                )
                for o in (resp.get("data") or [])
            ]
        except Exception as e:
            logger.error(f"AngelOne get_orders failed: {e}")
            return []

    # ── Safety ────────────────────────────────────────────────────────

    def kill_switch(self) -> list[OrderResponse]:
        """
        EMERGENCY: Cancel all open orders and close all positions at market.
        Runs even if some individual operations fail — logs and continues.
        """
        if not self.is_connected:
            logger.error("KILL SWITCH: not connected to AngelOne!")
            return []

        responses = []

        try:
            book = self._smart.orderBook()
            for o in (book.get("data") or []):
                if str(o.get("status", "")).lower() in ("open", "pending", "open pending", "trigger pending"):
                    try:
                        self._smart.cancelOrder(o["orderid"], o.get("variety", "NORMAL"))
                        logger.warning(f"KILL: cancelled order {o['orderid']}")
                    except Exception as e:
                        logger.error(f"KILL: cancel {o.get('orderid')} failed: {e}")
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
        """Return available margin / funds (AngelOne calls this RMS limits)."""
        if not self.is_connected:
            return {}
        try:
            return self._smart.rmsLimit()
        except Exception as e:
            logger.error(f"Margins fetch failed: {e}")
            return {}
