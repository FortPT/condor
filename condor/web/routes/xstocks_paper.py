"""Paper-only xStocks surface (Kraken public data), scoped to the Condor user."""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from condor.web.auth import get_current_user
from condor.web.models import WebUser
from condor.xstocks_paper import service

router = APIRouter(prefix="/xstocks-paper", tags=["xstocks-paper"])


class Prediction(BaseModel):
    symbol: str
    verdict: str
    target_pct: str
    stop_pct: str
    horizon_hours: str
    note: str = ""


def reject(error):
    raise HTTPException(status_code=400, detail=str(error)) from None


@router.get("/status")
async def status(user: WebUser = Depends(get_current_user)):
    return await service.status()


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
