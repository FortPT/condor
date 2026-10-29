"""Deposits and withdrawals of the native venues, scoped to the authenticated user.

The owner's record of money that crossed an account boundary, so a loop card's PnL
is a trading result and not a deposit. Nothing here moves money: it lists, adds,
confirms, dismisses and deletes *records*, and one route re-reads the venues' own
transfer history. See ``docs/native-flows.md``.
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from condor import native_accounts, native_flows, native_venues
from condor.web.auth import get_current_user
from condor.web.models import WebUser

log = logging.getLogger(__name__)

router = APIRouter(prefix="/native-flows", tags=["native-flows"])


class NewFlow(BaseModel):
    venue: str
    #: Signed: positive = deposit, negative = withdrawal.
    amount: float | str
    currency: str
    #: Epoch seconds or an ISO date/time (a bare date is noon UTC); default now.
    ts: float | str | None = None
    note: str = ""
    #: The USD value when the currency cannot be priced (or to override the rate).
    usd_value: float | str | None = None


class SyncRequest(BaseModel):
    venue: str | None = None
    lookback_days: int | None = None


def reject(error):
    raise HTTPException(status_code=400, detail=str(error)) from None


def _venues(key: str | None) -> list[native_venues.NativeVenue]:
    if key:
        try:
            venue = native_venues.get(key)
        except KeyError:
            raise HTTPException(status_code=404, detail="Unknown venue") from None
        if not venue.account or venue.paper:
            raise HTTPException(
                status_code=400,
                detail="This venue holds no account, so it has no deposits",
            )
        return [venue]
    return list(native_venues.account_venues())


@router.get("")
async def list_flows(
    venue: str | None = Query(None),
    include_dismissed: bool = Query(False),
    user: WebUser = Depends(get_current_user),
):
    """Recorded flows (newest first) plus each venue's summary and pending count."""
    venues = _venues(venue)
    try:
        flows = native_flows.list_flows(
            user.id, venue, include_dismissed=include_dismissed
        )
        summaries = []
        for item in venues:
            info = native_flows.summary(user.id, item.key)
            info["label"] = item.label
            info["note"] = native_flows.pending_note(info)
            summaries.append(info)
    except ValueError as error:
        reject(error)
    return {"flows": flows, "venues": summaries}


@router.post("")
async def add_flow(body: NewFlow, user: WebUser = Depends(get_current_user)):
    """Record a deposit (positive) or withdrawal (negative) by hand; it is confirmed at once."""
    _venues(body.venue)
    try:
        flow = await native_flows.add_manual(
            user.id,
            body.venue,
            amount=body.amount,
            currency=body.currency,
            ts=body.ts,
            note=body.note,
            usd_value=body.usd_value,
        )
    except ValueError as error:
        reject(error)
    return flow


@router.post("/sync")
async def sync_flows(
    body: SyncRequest | None = None, user: WebUser = Depends(get_current_user)
):
    """Re-read the venue's own transfer history and look for unexplained cash moves.

    Read-only on the exchange. A venue whose history cannot be read answers
    ``reason: "unavailable: ..."`` and detection plus manual entry still work.
    """
    body = body or SyncRequest()
    results = []
    for venue in _venues(body.venue):
        service = venue.service
        try:
            connected = bool(service.status(user.id)["connected"])
        except Exception:  # noqa: BLE001
            connected = False
        if not connected:
            results.append({"venue": venue.key, "ok": False, "reason": "not connected"})
            continue
        view = None
        try:
            view = await native_accounts.value_account(venue, user.id, max_age=0)
        except Exception as error:  # noqa: BLE001
            log.warning(
                "%s sync: account unreadable (%s)", venue.label, type(error).__name__
            )
        result = await native_flows.sync(
            user.id,
            venue.key,
            service,
            view=view,
            lookback_days=body.lookback_days,
            force=True,
        )
        if view is not None:
            try:
                ledger = service.status(user.id).get("ledger")
                found = native_flows.detect(
                    user.id, venue.key, view, ledger, now=time.time()
                )
                result["detected"] = len(found)
            except Exception as error:  # noqa: BLE001
                result["detected"] = 0
                log.warning(
                    "%s detection skipped (%s)", venue.label, type(error).__name__
                )
        results.append(result)
    return {"results": results}


def _change(flow_id: str, status: str, user: WebUser):
    try:
        return native_flows.set_status(user.id, flow_id, status)
    except KeyError:
        raise HTTPException(status_code=404, detail="Flow not found") from None
    except ValueError as error:
        reject(error)


@router.post("/{flow_id}/confirm")
async def confirm_flow(flow_id: str, user: WebUser = Depends(get_current_user)):
    return _change(flow_id, "confirmed", user)


@router.post("/{flow_id}/dismiss")
async def dismiss_flow(flow_id: str, user: WebUser = Depends(get_current_user)):
    return _change(flow_id, "dismissed", user)


@router.delete("/{flow_id}")
async def delete_flow(flow_id: str, user: WebUser = Depends(get_current_user)):
    """Delete an entry you added by hand (api and detected rows are dismissed instead)."""
    try:
        native_flows.delete_manual(user.id, flow_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Flow not found") from None
    except ValueError as error:
        reject(error)
    return {"deleted": flow_id}
