"""Euro conversions use the euro account rate, not the ticker pool's EUR-USDT."""

import asyncio

import pytest

from condor import market_rates, revolut_x

POOL = {"EUR-USDT": 1.143, "BTC-USDT": 64000.0}


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def pool(monkeypatch):
    async def fake_pool(server, connector=None):
        return dict(POOL)

    monkeypatch.setattr(market_rates, "get_price_pool", fake_pool)


def _set_rate(monkeypatch, rate, recent=None):
    async def eur_usd():
        return rate

    monkeypatch.setattr(revolut_x.service, "eur_usd", eur_usd)
    monkeypatch.setattr(revolut_x.service, "recent_eur_usd", lambda *a, **k: recent)


def test_euro_pairs_use_the_account_rate(pool, monkeypatch):
    _set_rate(monkeypatch, 1.117)
    rates = _run(
        market_rates.get_rates("s", ["EUR-USDT", "USDT-EUR", "EUR-USD", "BTC-USDT"])
    )
    assert rates["EUR-USDT"] == pytest.approx(1.117)
    assert rates["EUR-USD"] == pytest.approx(1.117)
    assert rates["USDT-EUR"] == pytest.approx(1 / 1.117)
    assert rates["BTC-USDT"] == 64000.0


def test_falls_back_to_the_pool_without_a_rate(pool, monkeypatch):
    _set_rate(monkeypatch, None)
    rates = _run(market_rates.get_rates("s", ["EUR-USDT"]))
    assert rates["EUR-USDT"] == pytest.approx(1.143)


def test_recent_rate_stands_in_when_the_fresh_one_is_missing(pool, monkeypatch):
    _set_rate(monkeypatch, None, recent=1.12)
    rates = _run(market_rates.get_rates("s", ["EUR-USDT"]))
    assert rates["EUR-USDT"] == pytest.approx(1.12)


def test_explicit_connector_keeps_its_own_price(pool, monkeypatch):
    _set_rate(monkeypatch, 1.117)
    rates = _run(market_rates.get_rates("s", ["EUR-USDT"], connector="binance"))
    assert rates["EUR-USDT"] == pytest.approx(1.143)


def test_pair_classification():
    assert market_rates._euro_pair("EUR-USDT") is True
    assert market_rates._euro_pair("USD-EUR") is False
    assert market_rates._euro_pair("EUR-BTC") is None
    assert market_rates._euro_pair("BTC-USDT") is None
