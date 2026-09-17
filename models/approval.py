"""
Approval Request Queue
────────────────────────
The actual replacement for Phase 0's blunt safety backstop
(strategy/intraday_agent.py's `_live_autofire_blocked()`, which just
refuses to fire at all once TRADE_MODE leaves "paper"). Once this exists,
a validated signal in live mode STAGES an approval request instead of
opening a position directly — a human reviews it (entry/stop/target plus
a snapshot of the strategy's Performance Evidence Bundle from
models/evidence_bundle.py) and explicitly approves or rejects it, within
a visible TTL countdown.

Re-validated at approval time, not just at staging time — price may have
moved, the strategy may have been auto-suspended
(models/strategy_registry.py) in the meantime, or the market may have
closed. Approving is not a blanket "yes, and also for later" — it's a
one-shot decision for this exact request.

In-memory only, matching this project's existing pattern for short-lived
state (backend/app.py's maintenance_job/backtest_progress,
intraday_agent's own _state are all in-memory too). An approval is meant
to be resolved within minutes; losing a pending request on a backend
restart is an acceptable, SAFE failure mode — nothing executes, and the
agent will simply stage a fresh request next cycle if the setup is still
valid.

This module knows nothing about positions, brokers, or the specific
agent — it only manages the approval lifecycle. strategy/intraday_agent.py
is responsible for actually opening a position once approve() succeeds.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional
from uuid import uuid4

from utils.logger import get_logger

logger = get_logger("approval")

PENDING = "PENDING"
APPROVED = "APPROVED"
REJECTED = "REJECTED"
EXPIRED = "EXPIRED"
VOIDED_STALE = "VOIDED_STALE"

# A 5-minute-candle entry zone doesn't stay meaningful much past this.
DEFAULT_TTL_SECS = 120

# How far the live price may drift from price_at_request before an
# approval is voided rather than honoured "close enough" — 3% of an
# option's premium is already a meaningfully different entry.
PRICE_TOLERANCE_PCT = 0.03


@dataclass
class ApprovalRequest:
    approval_id: str
    strategy: str
    symbol: str             # underlying, e.g. "NIFTY"
    mode: str               # "opening" | "latest" — see intraday_agent.py
    decision: dict          # raw TradeDecision-shaped dict: side, ce_symbol,
                             # pe_symbol, entry, partial, stop, confidence,
                             # tier, lot_size, exchange
    evidence_bundle: dict   # snapshot of the strategy's Performance Evidence
                             # Bundle at staging time — for the human reviewing
    price_at_request: float
    requested_at: datetime = field(default_factory=datetime.now)
    expires_at: datetime = field(init=False)
    status: str = PENDING
    resolved_at: Optional[datetime] = None
    resolution_reason: str = ""

    def __post_init__(self):
        self.expires_at = self.requested_at + timedelta(seconds=DEFAULT_TTL_SECS)


_lock = threading.RLock()
_queue: dict[str, ApprovalRequest] = {}


def stage(strategy: str, symbol: str, mode: str, decision: dict, evidence_bundle: dict) -> ApprovalRequest:
    with _lock:
        req = ApprovalRequest(
            approval_id=str(uuid4()),
            strategy=strategy, symbol=symbol, mode=mode, decision=decision,
            evidence_bundle=evidence_bundle, price_at_request=decision["entry"],
        )
        _queue[req.approval_id] = req
        logger.info(
            f"Approval staged: {req.approval_id[:8]} {symbol} {decision['side'].upper()} "
            f"@ {decision['entry']} (expires {req.expires_at.strftime('%H:%M:%S')})"
        )
        return req


def _expire_stale_locked():
    now = datetime.now()
    for req in _queue.values():
        if req.status == PENDING and now > req.expires_at:
            req.status = EXPIRED
            req.resolved_at = now
            req.resolution_reason = "TTL expired without a decision"
            logger.info(f"Approval {req.approval_id[:8]} EXPIRED (no decision within {DEFAULT_TTL_SECS}s)")


def list_pending() -> list[ApprovalRequest]:
    with _lock:
        _expire_stale_locked()
        return [r for r in _queue.values() if r.status == PENDING]


def get(approval_id: str) -> Optional[ApprovalRequest]:
    with _lock:
        _expire_stale_locked()
        return _queue.get(approval_id)


def reject(approval_id: str, reason: str = "rejected by user") -> Optional[ApprovalRequest]:
    with _lock:
        req = _queue.get(approval_id)
        if req is None or req.status != PENDING:
            return None
        req.status = REJECTED
        req.resolved_at = datetime.now()
        req.resolution_reason = reason
        logger.info(f"Approval {approval_id[:8]} REJECTED: {reason}")
        return req


def approve(
    approval_id: str,
    current_price: Optional[float],
    strategy_suspended: bool,
    market_open: bool,
) -> tuple[Optional[ApprovalRequest], str]:
    """
    Re-validates before marking APPROVED — this is the actual point of
    the whole module. Returns (request, "approved") on success, or
    (None, reason) if the request can't be approved right now. The caller
    (strategy/intraday_agent.py's approve_request()) is responsible for
    actually opening a position once this returns success.
    """
    with _lock:
        _expire_stale_locked()
        req = _queue.get(approval_id)
        if req is None:
            return None, "No such approval request"
        if req.status != PENDING:
            return None, f"Already {req.status.lower()}, cannot approve again"

        if not market_open:
            req.status = VOIDED_STALE
            req.resolved_at = datetime.now()
            req.resolution_reason = "market closed since request was staged"
            return None, req.resolution_reason

        if strategy_suspended:
            req.status = VOIDED_STALE
            req.resolved_at = datetime.now()
            req.resolution_reason = "strategy was suspended since request was staged"
            return None, req.resolution_reason

        # Fail SAFE, not open: if the current price can't be verified at
        # all, that's a reason to withhold approval, not a reason to skip
        # the check silently. Caught during testing — the original version
        # let current_price=None slide through and approve anyway, which
        # is exactly backwards for a check whose entire purpose is making
        # sure the price hasn't moved since staging. "Can't tell" must
        # never be treated as "didn't move".
        if not req.price_at_request or current_price is None:
            req.status = VOIDED_STALE
            req.resolved_at = datetime.now()
            req.resolution_reason = "could not verify current price — refusing to approve blind"
            return None, req.resolution_reason

        drift = abs(current_price - req.price_at_request) / req.price_at_request
        if drift > PRICE_TOLERANCE_PCT:
            req.status = VOIDED_STALE
            req.resolved_at = datetime.now()
            req.resolution_reason = (
                f"price moved {drift*100:.1f}% (tolerance {PRICE_TOLERANCE_PCT*100:.0f}%) "
                f"since request was staged ({req.price_at_request} -> {current_price})"
            )
            return None, req.resolution_reason

        req.status = APPROVED
        req.resolved_at = datetime.now()
        req.resolution_reason = "approved by user"
        logger.info(f"Approval {approval_id[:8]} APPROVED")
        return req, "approved"
