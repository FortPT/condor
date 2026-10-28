"""Kraken xStocks holdings join the dashboard for their owner without entering SDS."""

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from condor.crypto_com_exchange import service as exchange_service
from condor.kraken_xstocks import service as kraken_service
from condor.web.models import WebUser
from condor.web.routes import portfolio


def no_exchange(monkeypatch):
    monkeypatch.setattr(exchange_service, "status", lambda uid: {"connected": False})


@pytest.mark.asyncio
async def test_kraken_balances_are_user_scoped_and_never_written_to_shared_cache(
    monkeypatch,
):
    state = {"master": {"binance": [{"token": "BTC", "units": 1, "value": 100}]}}
    original = deepcopy(state)
    sds = type("SDS", (), {"get_or_fetch": AsyncMock(return_value=state)})()
    monkeypatch.setattr(portfolio, "get_server_data_service", lambda: sds)
    no_exchange(monkeypatch)
    monkeypatch.setattr(kraken_service, "status", lambda uid: {"connected": uid == 1})
    kraken = AsyncMock(
        return_value={
            "connector": "Kraken xStocks",
            "total_usd": 75,
            "balances": [
                {"token": "USD", "total": 40, "available": 35, "usd_value": 40},
                {"token": "AAPLx", "total": 0.1, "available": 0.1, "usd_value": 35},
            ],
            "note": "Only the bot allocation may be traded.",
        }
    )
    monkeypatch.setattr(kraken_service, "portfolio", kraken)
    owner = await portfolio.get_portfolio("vm08", False, WebUser(id=1, role="admin"))
    other = await portfolio.get_portfolio("vm08", False, WebUser(id=2, role="admin"))
    assert owner.total_usd == 175
    assert owner.connectors[-1].connector == "Kraken xStocks"
    assert owner.connectors[-1].balances[0].available == 35
    assert other.total_usd == 100
    assert other.notes == []
    kraken.assert_awaited_once_with(1)
    assert state == original


@pytest.mark.asyncio
async def test_kraken_failure_is_visible_without_hiding_other_connectors(monkeypatch):
    sds = type("SDS", (), {"get_or_fetch": AsyncMock(return_value={})})()
    monkeypatch.setattr(portfolio, "get_server_data_service", lambda: sds)
    no_exchange(monkeypatch)
    monkeypatch.setattr(kraken_service, "status", lambda uid: {"connected": True})
    monkeypatch.setattr(
        kraken_service,
        "portfolio",
        AsyncMock(side_effect=ValueError("unavailable")),
    )
    response = await portfolio.get_portfolio("vm08", False, WebUser(id=1, role="admin"))
    assert response.total_usd == 0
    assert response.connectors == []
    assert "Kraken xStocks could not be valued" in response.notes[0]
    assert "excluded from this total" in response.notes[0]


@pytest.mark.asyncio
async def test_an_unconnected_kraken_account_is_not_queried(monkeypatch):
    sds = type("SDS", (), {"get_or_fetch": AsyncMock(return_value={})})()
    monkeypatch.setattr(portfolio, "get_server_data_service", lambda: sds)
    no_exchange(monkeypatch)
    monkeypatch.setattr(kraken_service, "status", lambda uid: {"connected": False})
    kraken = AsyncMock()
    monkeypatch.setattr(kraken_service, "portfolio", kraken)
    response = await portfolio.get_portfolio("vm08", False, WebUser(id=1, role="admin"))
    assert response.connectors == []
    kraken.assert_not_awaited()


@pytest.mark.asyncio
async def test_kraken_xstocks_is_not_added_when_the_kraken_connector_already_counts_it(
    monkeypatch,
):
    state = {"master": {"kraken": [{"token": "EUR", "units": 9.37, "value": 10.4}]}}
    sds = type("SDS", (), {"get_or_fetch": AsyncMock(return_value=state)})()
    monkeypatch.setattr(portfolio, "get_server_data_service", lambda: sds)
    no_exchange(monkeypatch)
    monkeypatch.setattr(kraken_service, "status", lambda uid: {"connected": True})
    kraken = AsyncMock(
        return_value={
            "connector": "Kraken xStocks",
            "total_usd": 10.4,
            "balances": [],
            "note": "",
        }
    )
    monkeypatch.setattr(kraken_service, "portfolio", kraken)
    response = await portfolio.get_portfolio("vm08", False, WebUser(id=1, role="admin"))
    assert [c.connector for c in response.connectors] == ["kraken"]
    kraken.assert_not_awaited()
