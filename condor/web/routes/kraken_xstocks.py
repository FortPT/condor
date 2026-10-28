"""Native Kraken xStocks account/trading surface, scoped to the authenticated Condor user."""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, SecretStr

from condor.kraken_xstocks import service
from condor.web.auth import get_current_user
from condor.web.models import WebUser

router = APIRouter(prefix="/kraken-xstocks", tags=["kraken-xstocks"])


class Credentials(BaseModel):
    api_key: SecretStr
    api_secret: SecretStr


class Enable(BaseModel):
    enabled: bool


class OrderRequest(BaseModel):
    side: str
    symbol: str
    amount: str
    max_slippage_pct: str = ""


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
    return service.status(user.id)


@router.put("/credentials")
async def credentials(body: Credentials, user: WebUser = Depends(get_current_user)):
    try:
        service.save_credentials(
            user.id,
            body.api_key.get_secret_value(),
            body.api_secret.get_secret_value(),
        )
        return service.status(user.id)
    except ValueError as error:
        reject(error)


@router.delete("/credentials")
async def disconnect(user: WebUser = Depends(get_current_user)):
    return service.disconnect(user.id)


@router.put("/enabled")
async def enabled(body: Enable, user: WebUser = Depends(get_current_user)):
    try:
        return service.set_enabled(user.id, body.enabled)
    except ValueError as error:
        reject(error)


@router.post("/sync")
async def sync(user: WebUser = Depends(get_current_user)):
    try:
        return await service.sync_from_account(user.id)
    except ValueError as error:
        reject(error)


@router.get("/accounts")
async def accounts(user: WebUser = Depends(get_current_user)):
    try:
        return await service.accounts(user.id)
    except ValueError as error:
        reject(error)


@router.get("/valuation")
async def valuation(user: WebUser = Depends(get_current_user)):
    try:
        return await service.valuation(user.id)
    except ValueError as error:
        reject(error)


@router.get("/history")
async def history(user: WebUser = Depends(get_current_user)):
    try:
        return await service.history(user.id)
    except ValueError as error:
        reject(error)


@router.get("/coins")
async def coins(
    keyword: str = Query("", max_length=40),
    user: WebUser = Depends(get_current_user),
):
    try:
        return await service.coins(user.id, keyword)
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
        return await service.candles(user.id, symbol, timeframe, count)
    except ValueError as error:
        reject(error)


@router.get("/book")
async def book(
    symbol: str = Query(..., max_length=12),
    depth: int = Query(10, ge=1, le=50),
    user: WebUser = Depends(get_current_user),
):
    try:
        return await service.book(user.id, symbol, depth)
    except ValueError as error:
        reject(error)


@router.post("/preview")
async def preview(body: OrderRequest, user: WebUser = Depends(get_current_user)):
    try:
        return await service.preview(
            user.id, body.side, body.symbol, body.amount, body.max_slippage_pct or None
        )
    except ValueError as error:
        reject(error)


@router.post("/order")
async def order(body: OrderRequest, user: WebUser = Depends(get_current_user)):
    try:
        return await service.order(
            user.id, body.side, body.symbol, body.amount, body.max_slippage_pct or None
        )
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
        return await service.scorecard(user.id)
    except ValueError as error:
        reject(error)


@router.post("/reconcile")
async def reconcile(user: WebUser = Depends(get_current_user)):
    try:
        return await service.reconcile(user.id)
    except ValueError as error:
        reject(error)
