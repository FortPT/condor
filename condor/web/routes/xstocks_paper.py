"""Paper xStocks surface (Kraken public data and a simulated account), scoped to the Condor user.

Everything here is a simulation: the account is a ledger of simulated cash and
positions filled against Kraken's public order book (:mod:`condor.xstocks_paper_ledger`).
Resetting it is an admin-only REST action and is not exposed to the MCP tool.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from condor.web.auth import get_current_user, require_admin
from condor.web.models import WebUser
from condor.xstocks_paper import service
from condor.xstocks_paper_ledger import service as account

router = APIRouter(prefix="/xstocks-paper", tags=["xstocks-paper"])


class Prediction(BaseModel):
    symbol: str
    verdict: str
    target_pct: str
    stop_pct: str
    horizon_hours: str
    note: str = ""


class OrderRequest(BaseModel):
    side: str
    symbol: str
    amount: str
    max_slippage_pct: str = ""
    client_id: str = ""


def reject(error):
    raise HTTPException(status_code=400, detail=str(error)) from None


@router.get("/status")
async def status(user: WebUser = Depends(get_current_user)):
    base = await service.status()
    return {**base, **account.status(user.id)}


@router.get("/coins")
async def coins(
    keyword: str = Query("", max_length=40),
    user: WebUser = Depends(get_current_user),
):
    try:
        return await service.coins(keyword, user.id)
    except ValueError as error:
        reject(error)


@router.get("/candles")
async def candles(
    symbol: str = Query(..., max_length=12),
    timeframe: str = Query("15m", max_length=3),
    count: int = Query(96, ge=1, le=300),
    user: WebUser = Depends(get_current_user),
):
    try:
        return await service.candles(symbol, timeframe, count)
    except ValueError as error:
        reject(error)


@router.get("/accounts")
async def accounts(user: WebUser = Depends(get_current_user)):
    return await account.accounts(user.id)


@router.get("/valuation")
async def valuation(user: WebUser = Depends(get_current_user)):
    try:
        return await account.valuation(user.id)
    except ValueError as error:
        reject(error)


@router.get("/history")
async def history(user: WebUser = Depends(get_current_user)):
    return await account.history(user.id)


@router.post("/preview")
async def preview(body: OrderRequest, user: WebUser = Depends(get_current_user)):
    try:
        return await account.preview(
            user.id, body.side, body.symbol, body.amount, body.max_slippage_pct or None
        )
    except ValueError as error:
        reject(error)


@router.post("/order")
async def order(body: OrderRequest, user: WebUser = Depends(get_current_user)):
    try:
        return await account.order(
            user.id,
            body.side,
            body.symbol,
            body.amount,
            body.max_slippage_pct or None,
            body.client_id or None,
        )
    except ValueError as error:
        reject(error)


@router.post("/reset")
async def reset(user: WebUser = Depends(get_current_user)):
    """Archive the simulated ledger and start again from the starting cash (admin only)."""
    require_admin(user)
    try:
        return await account.reset(user.id)
    except ValueError as error:
        reject(error)


@router.post("/predict")
async def predict(body: Prediction, user: WebUser = Depends(get_current_user)):
    try:
        return await service.predict(
            user.id,
            body.symbol,
            body.verdict,
            body.target_pct,
            body.stop_pct,
            body.horizon_hours,
            body.note,
        )
    except ValueError as error:
        reject(error)


@router.get("/scorecard")
async def scorecard(user: WebUser = Depends(get_current_user)):
    try:
        return service.scorecard(user.id)
    except ValueError as error:
        reject(error)
