"""App holdings join the dashboard for their owner without entering SDS."""

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from condor.web.models import WebUser
from condor.web.routes import portfolio


@pytest.mark.asyncio
async def test_app_balances_are_user_scoped_and_never_written_to_shared_cache(
    monkeypatch,
):
    state = {"master": {"binance": [{"token": "BTC", "units": 1, "value": 100}]}}
    original = deepcopy(state)
    sds = type("SDS", (), {"get_or_fetch": AsyncMock(return_value=state)})()
    monkeypatch.setattr(portfolio, "get_server_data_service", lambda: sds)
    monkeypatch.setattr(
        portfolio.app_service, "status", lambda uid: {"connected": uid == 1}
    )
    app = AsyncMock(
        return_value={
            "connector": "Crypto.com App",
            "total_usd": 22,
            "balances": [
                {"token": "CRO", "total": 200, "available": 190, "usd_value": 22}
            ],
            "note": "Only the bot allocation may be traded.",
        }
    )
    monkeypatch.setattr(portfolio.app_service, "portfolio", app)
    owner = await portfolio.get_portfolio("vm08", False, WebUser(id=1, role="admin"))
    other = await portfolio.get_portfolio("vm08", False, WebUser(id=2, role="admin"))
    assert owner.total_usd == 122
    assert owner.connectors[-1].balances[0].available == 190
    assert other.total_usd == 100
    assert other.notes == []
    app.assert_awaited_once_with(1)
    assert state == original


@pytest.mark.asyncio
async def test_app_failure_is_visible_without_hiding_other_connectors(monkeypatch):
    sds = type("SDS", (), {"get_or_fetch": AsyncMock(return_value={})})()
    monkeypatch.setattr(portfolio, "get_server_data_service", lambda: sds)
    monkeypatch.setattr(
        portfolio.app_service, "status", lambda uid: {"connected": True}
    )
    monkeypatch.setattr(
        portfolio.app_service,
        "portfolio",
        AsyncMock(side_effect=ValueError("unavailable")),
    )
    response = await portfolio.get_portfolio("vm08", False, WebUser(id=1, role="admin"))
    assert response.total_usd == 0
    assert response.connectors == []
    assert "excluded from this total" in response.notes[0]
