"""Revolut X holdings join the dashboard for their owner without entering SDS."""

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from condor import portfolio_history
from condor.crypto_com_exchange import service as exchange_service
from condor.kraken_xstocks import service as kraken_service
from condor.revolut_x import service as revolut_service
from condor.web.models import WebUser
from condor.web.routes import portfolio


def no_other_native_accounts(monkeypatch):
    monkeypatch.setattr(exchange_service, "status", lambda uid: {"connected": False})
    monkeypatch.setattr(kraken_service, "status", lambda uid: {"connected": False})


@pytest.mark.asyncio
async def test_revolut_x_balances_are_user_scoped_and_never_written_to_shared_cache(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("CONDOR_DATA_DIR", str(tmp_path))
    state = {"master": {"binance": [{"token": "BTC", "units": 1, "value": 100}]}}
    original = deepcopy(state)
    sds = type("SDS", (), {"get_or_fetch": AsyncMock(return_value=state)})()
    monkeypatch.setattr(portfolio, "get_server_data_service", lambda: sds)
    no_other_native_accounts(monkeypatch)
    monkeypatch.setattr(revolut_service, "status", lambda uid: {"connected": uid == 1})
    revolut = AsyncMock(
        return_value={
            "connector": "Revolut X",
            "total_usd": 75,
            "balances": [
                {"token": "EUR", "total": 40, "available": 35, "usd_value": 44},
                {"token": "SUI", "total": 10, "available": 10, "usd_value": 31},
            ],
            "note": "Only the bot allocation may be traded.",
        }
    )
    monkeypatch.setattr(revolut_service, "portfolio", revolut)
    owner = await portfolio.get_portfolio("vm08", False, WebUser(id=1, role="admin"))
    other = await portfolio.get_portfolio("vm08", False, WebUser(id=2, role="admin"))
    assert owner.total_usd == 175
    assert owner.connectors[-1].connector == "Revolut X"
    assert owner.connectors[-1].balances[0].available == 35
    assert other.total_usd == 100
    assert other.notes == []
    revolut.assert_awaited_once_with(1)
    assert state == original
    # the dashboard read also feeds the Portfolio chart
    rows = portfolio_history.series(1, since=0)
    assert rows[portfolio_history.REVOLUT][0][1] == 75


@pytest.mark.asyncio
async def test_revolut_x_failure_is_visible_without_hiding_other_connectors(
    monkeypatch,
):
    sds = type("SDS", (), {"get_or_fetch": AsyncMock(return_value={})})()
    monkeypatch.setattr(portfolio, "get_server_data_service", lambda: sds)
    no_other_native_accounts(monkeypatch)
    monkeypatch.setattr(revolut_service, "status", lambda uid: {"connected": True})
    monkeypatch.setattr(
        revolut_service,
        "portfolio",
        AsyncMock(side_effect=ValueError("unavailable")),
    )
    response = await portfolio.get_portfolio("vm08", False, WebUser(id=1, role="admin"))
    assert response.total_usd == 0
    assert response.connectors == []
    assert "Revolut X could not be valued" in response.notes[0]
    assert "excluded from this total" in response.notes[0]


@pytest.mark.asyncio
async def test_an_unconnected_revolut_x_account_is_not_queried(monkeypatch):
    sds = type("SDS", (), {"get_or_fetch": AsyncMock(return_value={})})()
    monkeypatch.setattr(portfolio, "get_server_data_service", lambda: sds)
    no_other_native_accounts(monkeypatch)
    monkeypatch.setattr(revolut_service, "status", lambda uid: {"connected": False})
    revolut = AsyncMock()
    monkeypatch.setattr(revolut_service, "portfolio", revolut)
    response = await portfolio.get_portfolio("vm08", False, WebUser(id=1, role="admin"))
    assert response.connectors == []
    revolut.assert_not_awaited()


@pytest.mark.asyncio
async def test_revolut_x_is_added_even_when_a_hummingbot_connector_is_named_revolut(
    monkeypatch, tmp_path
):
    """Hummingbot has no Revolut X connector, so nothing else counts this account."""
    monkeypatch.setenv("CONDOR_DATA_DIR", str(tmp_path))
    state = {"master": {"kraken": [{"token": "EUR", "units": 9.37, "value": 10.4}]}}
    sds = type("SDS", (), {"get_or_fetch": AsyncMock(return_value=state)})()
    monkeypatch.setattr(portfolio, "get_server_data_service", lambda: sds)
    no_other_native_accounts(monkeypatch)
    monkeypatch.setattr(revolut_service, "status", lambda uid: {"connected": True})
    revolut = AsyncMock(
        return_value={
            "connector": "Revolut X",
            "total_usd": 12,
            "balances": [],
            "note": "",
        }
    )
    monkeypatch.setattr(revolut_service, "portfolio", revolut)
    response = await portfolio.get_portfolio("vm08", False, WebUser(id=1, role="admin"))
    assert [c.connector for c in response.connectors] == ["kraken", "Revolut X"]
    assert response.total_usd == pytest.approx(22.4)
