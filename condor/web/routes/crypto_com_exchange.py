"""Native Exchange account/trading surface, scoped to the authenticated Condor user."""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, SecretStr

from condor.crypto_com_exchange import service
from condor.web.auth import get_current_user
from condor.web.models import WebUser

router = APIRouter(prefix="/crypto-exchange", tags=["crypto-exchange"])


class Credentials(BaseModel):
    api_key: SecretStr
    api_secret: SecretStr
    environment: str = "production"


class Enable(BaseModel):
    enabled: bool


class Sync(BaseModel):
    quote: str | None = None


class MakerMode(BaseModel):
    enabled: bool


class OrderRequest(BaseModel):
    side: str
    symbol: str
    amount: str
    max_slippage_pct: str = ""
    mode: str = "taker"
    purpose: str = ""
    maker_wait_sec: str = ""


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
            body.environment,
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
async def sync(body: Sync, user: WebUser = Depends(get_current_user)):
    try:
        return await service.sync_from_account(user.id, body.quote)
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
    keyword: str = Query("", max_length=100),
    user: WebUser = Depends(get_current_user),
):
    try:
        return await service.coins(user.id, keyword)
    except ValueError as error:
        reject(error)


@router.get("/candles")
async def candles(
    symbol: str = Query(..., max_length=24),
    timeframe: str = Query("1h", max_length=4),
    count: int = Query(100, ge=1, le=300),
    user: WebUser = Depends(get_current_user),
):
    try:
        return await service.candles(user.id, symbol, timeframe, count)
    except ValueError as error:
        reject(error)


@router.get("/book")
async def book(
    symbol: str = Query(..., max_length=24),
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
            user.id,
            body.side,
            body.symbol,
            body.amount,
            body.max_slippage_pct or None,
            mode=body.mode,
            purpose=body.purpose or None,
            maker_wait_sec=body.maker_wait_sec or None,
        )
    except ValueError as error:
        reject(error)


@router.post("/order")
async def order(body: OrderRequest, user: WebUser = Depends(get_current_user)):
    try:
        return await service.order(
            user.id,
            body.side,
            body.symbol,
            body.amount,
            body.max_slippage_pct or None,
            mode=body.mode,
            purpose=body.purpose or None,
            maker_wait_sec=body.maker_wait_sec or None,
        )
    except ValueError as error:
        reject(error)


@router.get("/maker-stats")
async def maker_stats(user: WebUser = Depends(get_current_user)):
    """Read-only: how maker orders have done (fill rate, fees, time to fill)."""
    return service.maker_stats(user.id)


@router.put("/maker-mode")
async def maker_mode(body: MakerMode, user: WebUser = Depends(get_current_user)):
    """Owner switch for maker orders; off by default and not an MCP action."""
    return service.set_maker_enabled(user.id, body.enabled)


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


@router.post("/reconcile")
async def reconcile(user: WebUser = Depends(get_current_user)):
    try:
        return await service.reconcile(user.id)
    except ValueError as error:
        reject(error)
