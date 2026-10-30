"""Native Trading 212 account/trading surface, scoped to the authenticated Condor user.

The API key and secret are write-only: they are accepted here and never sent back. The
Trading 212 API has no withdrawal endpoint, and nothing here can reach one.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, SecretStr

from condor import native_performance
from condor.trading212 import service
from condor.web.auth import get_current_user
from condor.web.models import WebUser
from condor.xstocks_market import us_market_open

router = APIRouter(prefix="/trading212", tags=["trading212"])


class Credentials(BaseModel):
    # Validated in the service, not here: a failed field constraint would echo the
    # submitted value back in a 422 body. An empty secret means a legacy single key.
    api_key: SecretStr
    api_secret: SecretStr = SecretStr("")


class MarketDataKey(BaseModel):
    # Alpaca market data only (no brokerage): validated in the service for the same
    # reason as above, and never sent back.
    key_id: SecretStr
    secret: SecretStr


class Enable(BaseModel):
    enabled: bool


class Excluded(BaseModel):
    # Validated in the service: plain symbols (MRNA) or Trading 212 tickers (MRNA_US_EQ).
    tickers: list[str]


class OrderRequest(BaseModel):
    side: str
    symbol: str
    amount: str
    max_slippage_pct: str = ""
    order_type: str = "limit"


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


@router.get("/market")
async def market_clock(user: WebUser = Depends(get_current_user)):
    """The regular US session clock; needs no key and cannot move money."""
    return {"venue": "Trading 212", "quote": "EUR", "market": us_market_open()}


@router.put("/credentials")
async def credentials(body: Credentials, user: WebUser = Depends(get_current_user)):
    try:
        return service.save_credentials(
            user.id, body.api_key.get_secret_value(), body.api_secret.get_secret_value()
        )
    except ValueError as error:
        reject(error)


@router.delete("/credentials")
async def disconnect(user: WebUser = Depends(get_current_user)):
    return service.disconnect(user.id)


@router.put("/market-data/credentials")
async def market_data_credentials(
    body: MarketDataKey, user: WebUser = Depends(get_current_user)
):
    """Alpaca's market data key: checked against Alpaca first, saved only when it works.

    It can read prices and nothing else here; it never touches an order or the account.
    """
    try:
        return await service.save_feed_credentials(
            user.id, body.key_id.get_secret_value(), body.secret.get_secret_value()
        )
    except ValueError as error:
        reject(error)


@router.delete("/market-data/credentials")
async def market_data_disconnect(user: WebUser = Depends(get_current_user)):
    """Back to Kraken's public xStocks data."""
    return service.disconnect_feed(user.id)


@router.put("/enabled")
async def enabled(body: Enable, user: WebUser = Depends(get_current_user)):
    try:
        if body.enabled:
            # Enabling first checks the bot ledger against the real account.
            return await service.enable_checked(user.id)
        return service.set_enabled(user.id, False)
    except ValueError as error:
        reject(error)


@router.put("/excluded")
async def excluded(body: Excluded, user: WebUser = Depends(get_current_user)):
    """The owner's own holdings the bot never adopts or trades; changing it pauses trading."""
    try:
        return await service.set_excluded(user.id, body.tickers)
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


@router.get("/performance")
async def performance(user: WebUser = Depends(get_current_user)):
    """What this venue earns and loses, from its recorded ledger and value history."""
    return await native_performance.report("trading212", user.id)


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


@router.get("/stocks")
async def stocks(
    keyword: str = Query("", max_length=40),
    user: WebUser = Depends(get_current_user),
):
    try:
        return await service.stocks(user.id, keyword)
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


@router.post("/preview")
async def preview(body: OrderRequest, user: WebUser = Depends(get_current_user)):
    try:
        return await service.preview(
            user.id,
            body.side,
            body.symbol,
            body.amount,
            body.max_slippage_pct or None,
            body.order_type,
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
            body.order_type,
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
