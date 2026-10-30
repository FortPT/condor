"""The /trading212 routes: credentials are write-only, errors are plain 400s, no network."""

import json

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from condor import trading212 as t212
from condor.web.auth import get_current_user
from condor.web.models import WebUser
from condor.web.routes import trading212 as routes

KEY = "key-id-1234567890"
SECRET = "secret-abcdefghijklmnop"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDOR_DATA_DIR", str(tmp_path))
    t212.service._forget(5)
    yield
    t212.service._forget(5)


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_current_user] = lambda: WebUser(
        id=5, username="u", first_name="U", role="user"
    )
    return TestClient(app)


def test_every_route_requires_the_signed_in_user():
    app = FastAPI()
    app.include_router(routes.router)
    anonymous = TestClient(app)
    assert anonymous.get("/trading212/status").status_code in (401, 403)
    assert anonymous.post("/trading212/order", json={}).status_code in (401, 403, 422)
    assert anonymous.put("/trading212/credentials", json={}).status_code in (
        401,
        403,
        422,
    )


def test_the_credentials_are_write_only_and_never_echoed(client):
    saved = client.put(
        "/trading212/credentials", json={"api_key": KEY, "api_secret": SECRET}
    )
    assert saved.status_code == 200
    assert saved.json()["connected"] is True
    assert saved.json()["has_secret"] is True
    for response in (saved, client.get("/trading212/status")):
        text = json.dumps(response.json())
        assert KEY not in text and SECRET not in text
    stored = json.loads(t212.credentials_path(5).read_text())
    assert stored == {"api_key": KEY, "api_secret": SECRET}
    gone = client.delete("/trading212/credentials")
    assert gone.json()["connected"] is False


def test_a_bad_credential_is_a_400_that_does_not_repeat_it(client):
    bad = client.put("/trading212/credentials", json={"api_key": "a:b" + "x" * 30})
    assert bad.status_code == 400
    assert "x" * 30 not in bad.text
    missing = client.put("/trading212/credentials", json={})
    assert missing.status_code == 422
    assert KEY not in missing.text


def test_enabling_needs_a_synced_eur_account_and_says_why(client):
    client.put("/trading212/credentials", json={"api_key": KEY, "api_secret": SECRET})
    refused = client.put("/trading212/enabled", json={"enabled": True})
    assert refused.status_code == 400
    assert "Sync" in refused.json()["detail"]


def test_enabling_goes_through_the_ledger_against_account_check(client, monkeypatch):
    client.put("/trading212/credentials", json={"api_key": KEY, "api_secret": SECRET})
    calls = []

    async def refused(user_id):
        calls.append(user_id)
        raise ValueError("The bot ledger and the Trading 212 account disagree")

    monkeypatch.setattr(t212.service, "enable_checked", refused)
    response = client.put("/trading212/enabled", json={"enabled": True})
    assert response.status_code == 400
    assert "disagree" in response.json()["detail"]
    assert calls == [5]
    # Pausing never needs the account.
    paused = client.put("/trading212/enabled", json={"enabled": False})
    assert paused.status_code == 200 and paused.json()["enabled"] is False
    assert calls == [5]


def test_the_exclusion_list_is_owner_editable_and_validated(client):
    saved = client.put("/trading212/excluded", json={"tickers": ["mrna", "AAPL_US_EQ"]})
    assert saved.status_code == 200
    assert saved.json()["excluded_tickers"] == ["AAPL", "MRNA"]
    assert client.get("/trading212/status").json()["excluded_tickers"] == [
        "AAPL",
        "MRNA",
    ]
    bad = client.put("/trading212/excluded", json={"tickers": ["not a symbol"]})
    assert bad.status_code == 400
    assert client.put("/trading212/excluded", json={}).status_code == 422
    assert (
        client.put("/trading212/excluded", json={"tickers": []}).json()[
            "excluded_tickers"
        ]
        == []
    )


def test_the_session_clock_needs_no_key(client):
    clock = client.get("/trading212/market").json()
    assert clock["venue"] == "Trading 212"
    assert set(clock["market"]) >= {"open", "new_york_time", "minutes_to_close"}


def test_orders_are_refused_as_a_400_while_paused_and_never_reach_trading212(client):
    client.put("/trading212/credentials", json={"api_key": KEY, "api_secret": SECRET})
    response = client.post(
        "/trading212/order",
        json={"side": "buy", "symbol": "AAPL", "amount": "8", "order_type": "limit"},
    )
    assert response.status_code == 400
    assert "paused" in response.json()["detail"]


def test_the_routes_offer_no_way_to_withdraw_or_bypass_the_cost_guard():
    paths = {route.path for route in routes.router.routes}
    assert not any("withdraw" in p or "transfer" in p for p in paths)
    assert "allow_costly_buy" not in routes.OrderRequest.model_fields
    assert {
        "/trading212/status",
        "/trading212/credentials",
        "/trading212/enabled",
        "/trading212/excluded",
        "/trading212/sync",
        "/trading212/accounts",
        "/trading212/valuation",
        "/trading212/history",
        "/trading212/stocks",
        "/trading212/candles",
        "/trading212/preview",
        "/trading212/order",
        "/trading212/predict",
        "/trading212/scorecard",
        "/trading212/reconcile",
    } <= paths


def _alpaca(monkeypatch, http=200):
    from tests import test_alpaca_market as alp

    fake = alp.FakeAlpaca({"SPY": alp.snap()}, http)
    monkeypatch.setattr(t212.service, "alpaca", fake)
    return alp, fake


def test_the_alpaca_price_key_is_checked_saved_write_only_and_removable(
    client, monkeypatch
):
    alp, fake = _alpaca(monkeypatch)
    assert client.get("/trading212/status").json()["market_data"]["source"] == "kraken"
    saved = client.put(
        "/trading212/market-data/credentials",
        json={"key_id": alp.KEY, "secret": alp.SECRET},
    )
    assert saved.status_code == 200
    assert saved.json()["market_data"] == {
        "source": "alpaca",
        "label": "Alpaca real-time IEX data",
        "alpaca_configured": True,
    }
    for response in (saved, client.get("/trading212/status")):
        text = json.dumps(response.json())
        assert alp.KEY not in text and alp.SECRET not in text
    assert fake.sent[0][1]["symbols"] == "SPY"  # checked before it was saved
    removed = client.delete("/trading212/market-data/credentials")
    assert removed.status_code == 200
    assert removed.json()["market_data"]["source"] == "kraken"
    assert not fake.configured(5)


def test_a_bad_or_malformed_alpaca_key_is_a_plain_400_and_is_not_saved(
    client, monkeypatch
):
    alp, fake = _alpaca(monkeypatch, http=403)
    rejected = client.put(
        "/trading212/market-data/credentials",
        json={"key_id": alp.KEY, "secret": alp.SECRET},
    )
    assert rejected.status_code == 400
    assert "rejected the key" in rejected.json()["detail"]
    assert alp.KEY not in rejected.text and alp.SECRET not in rejected.text
    malformed = client.put(
        "/trading212/market-data/credentials",
        json={"key_id": "x", "secret": "y z"},
    )
    assert malformed.status_code == 400
    assert "Invalid Alpaca key" in malformed.json()["detail"]
    assert "y z" not in malformed.text
    assert not fake.configured(5)


def test_the_alpaca_price_key_routes_require_the_signed_in_user():
    app = FastAPI()
    app.include_router(routes.router)
    anonymous = TestClient(app)
    for call in (anonymous.put, anonymous.delete):
        assert call("/trading212/market-data/credentials").status_code in (
            401,
            403,
            422,
        )
