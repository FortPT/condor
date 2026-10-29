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
