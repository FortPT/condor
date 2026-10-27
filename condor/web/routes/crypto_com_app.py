"""Native App account/trading surface, scoped to the authenticated Condor user."""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, SecretStr

from condor.crypto_com_app import service
from condor.web.auth import get_current_user
from condor.web.models import WebUser

router = APIRouter(prefix="/crypto-com", tags=["crypto-com"])


class Credentials(BaseModel):
    api_key: SecretStr
    api_secret: SecretStr


class Enable(BaseModel):
    enabled: bool


class Quote(BaseModel):
    kind: str
    source: str
    target: str
    amount: str


class Execute(BaseModel):
    quotation_id: str


def reject(error):
    raise HTTPException(status_code=400, detail=str(error)) from None


@router.get("/status")
async def status(user: WebUser = Depends(get_current_user)):
    return service.status(user.id)


@router.put("/credentials")
async def credentials(body: Credentials, user: WebUser = Depends(get_current_user)):
    try:
        service.save_credentials(
            user.id, body.api_key.get_secret_value(), body.api_secret.get_secret_value()
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


@router.post("/quote")
async def quote(body: Quote, user: WebUser = Depends(get_current_user)):
    try:
        return await service.quote(
            user.id, body.kind, body.source, body.target, body.amount
        )
    except ValueError as error:
        reject(error)


@router.post("/execute")
async def execute(body: Execute, user: WebUser = Depends(get_current_user)):
    try:
        return await service.execute(user.id, body.quotation_id)
    except ValueError as error:
        reject(error)


@router.post("/reconcile")
async def reconcile(user: WebUser = Depends(get_current_user)):
    try:
        return await service.reconcile(user.id)
    except ValueError as error:
        reject(error)
