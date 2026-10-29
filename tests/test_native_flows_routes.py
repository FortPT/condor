"""The /native-flows routes: list, add by hand, confirm, dismiss, delete, re-sync.

Fake services and a temp data dir: no exchange is contacted.
"""

import time
from decimal import Decimal

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from condor import native_accounts as na
from condor import native_flows as nf
from condor.crypto_com_exchange import service as exchange_service
from condor.revolut_x import service as revolut_service
from condor.web.auth import get_current_user
from condor.web.models import WebUser
from condor.web.routes import native_flows as routes

USERS = {"id": 11}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDOR_DATA_DIR", str(tmp_path))
    na.forget()
    USERS["id"] = 11
    yield
    na.forget()


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_current_user] = lambda: WebUser(
        id=USERS["id"], username="u", first_name="U", role="user"
    )
    return TestClient(app)


def post(client, **body):
    base = {
        "venue": "revolut_x",
        "amount": "4.27",
        "currency": "EUR",
        "ts": "2026-10-28",
    }
    return client.post("/native-flows", json={**base, **body})


def test_a_manual_deposit_is_priced_at_the_venues_rate_and_listed(client, monkeypatch):
    async def eur_usd():
        return Decimal("1.1")

    monkeypatch.setattr(revolut_service, "eur_usd", eur_usd)
    res = post(client, note="Top-up from Revolut")
    assert res.status_code == 200
    flow = res.json()
    assert (flow["amount"], flow["currency"], flow["source"], flow["status"]) == (
        "4.27",
        "EUR",
        "manual",
        "confirmed",
    )
    assert flow["usd_value_at_time"] == pytest.approx(4.697)
    assert flow["note"] == "Top-up from Revolut"

    listing = client.get("/native-flows", params={"venue": "revolut_x"}).json()
    assert [f["id"] for f in listing["flows"]] == [flow["id"]]
    (summary,) = listing["venues"]
    assert summary["venue"] == "revolut_x" and summary["label"] == "Revolut X"
    assert summary["recorded_net_flows_usd"] == pytest.approx(4.7)
    assert summary["tracked"] is True and summary["pending_count"] == 0


def test_a_withdrawal_is_negative_and_an_explicit_usd_value_is_honoured(client):
    res = post(client, amount="-3", currency="DOGE", usd_value="0.9", ts=None)
    assert res.status_code == 200
    assert res.json()["usd_value_at_time"] == -0.9


def test_a_currency_that_cannot_be_priced_asks_for_the_usd_value(client):
    res = post(client, venue="crypto_com_exchange", currency="DOGE")
    assert res.status_code == 400
    assert "enter its USD value" in res.json()["detail"]


@pytest.mark.parametrize(
    "body",
    [
        {"amount": "0"},
        {"amount": "abc"},
        {"currency": "no good"},
        {"ts": "yesterday-ish"},
        {"ts": "2099-01-01"},
    ],
)
def test_bad_input_is_a_400_with_a_reason(client, body):
    res = post(client, **({"usd_value": 1} | body))
    assert res.status_code == 400 and res.json()["detail"]


def test_an_unknown_venue_is_a_404(client):
    assert post(client, venue="nope").status_code == 404
    assert client.get("/native-flows", params={"venue": "nope"}).status_code == 404


def test_confirm_dismiss_delete(client):
    detected, _ = nf.add_flow(
        11,
        "revolut_x",
        amount="12",
        currency="EUR",
        ts=time.time() - 60,
        usd_value=13,
        source="detected",
        status="pending",
    )
    listing = client.get("/native-flows").json()
    other = next(v for v in listing["venues"] if v["venue"] == "revolut_x")
    assert other["pending_count"] == 1 and "Unconfirmed cash movement" in other["note"]

    assert (
        client.post(f"/native-flows/{detected['id']}/confirm").json()["status"]
        == "confirmed"
    )
    assert (
        client.post(f"/native-flows/{detected['id']}/dismiss").json()["status"]
        == "dismissed"
    )
    assert client.get("/native-flows").json()["flows"] == []
    shown = client.get("/native-flows", params={"include_dismissed": True}).json()
    assert len(shown["flows"]) == 1

    # A detected or api row is dismissed, never deleted; a manual one can be deleted.
    assert client.delete(f"/native-flows/{detected['id']}").status_code == 400
    manual = post(client, usd_value=5).json()
    assert client.delete(f"/native-flows/{manual['id']}").json() == {
        "deleted": manual["id"]
    }
    assert client.delete(f"/native-flows/{manual['id']}").status_code == 404
    assert client.post("/native-flows/" + "0" * 32 + "/confirm").status_code == 404
    assert client.post("/native-flows/not-an-id/confirm").status_code == 404


def test_one_users_flows_are_invisible_to_another(client):
    flow = post(client, usd_value=5).json()
    USERS["id"] = 12
    assert client.get("/native-flows").json()["flows"] == []
    assert client.post(f"/native-flows/{flow['id']}/dismiss").status_code == 404
    assert client.delete(f"/native-flows/{flow['id']}").status_code == 404


def test_sync_reports_a_venue_that_is_not_connected(client, monkeypatch):
    monkeypatch.setattr(exchange_service, "status", lambda uid: {"connected": False})
    res = client.post("/native-flows/sync", json={"venue": "crypto_com_exchange"})
    assert res.status_code == 200
    assert res.json()["results"] == [
        {"venue": "crypto_com_exchange", "ok": False, "reason": "not connected"}
    ]


def test_sync_reads_the_history_and_runs_detection(client, monkeypatch):
    calls = []

    async def private(user_id, method, params=None):
        calls.append(method)
        return (
            {"deposit_list": []}
            if method == "get-deposit-history"
            else {"withdrawal_list": []}
        )

    state = {"cash": 20.0}

    async def portfolio(user_id):
        return {
            "total_usd": state["cash"],
            "balances": [
                {
                    "token": "USD",
                    "total": state["cash"],
                    "available": 1,
                    "usd_value": state["cash"],
                }
            ],
            "note": "",
        }

    monkeypatch.setattr(
        exchange_service,
        "status",
        lambda uid: {"connected": True, "ledger": {"quote": "USD", "orders": []}},
    )
    monkeypatch.setattr(exchange_service, "private", private)
    monkeypatch.setattr(exchange_service, "portfolio", portfolio)

    first = client.post(
        "/native-flows/sync", json={"venue": "crypto_com_exchange"}
    ).json()
    (result,) = first["results"]
    assert result["ok"] is True and result["detected"] == 0
    assert calls == ["get-deposit-history", "get-withdrawal-history"]

    state["cash"] = 33.0
    na.forget()
    second = client.post(
        "/native-flows/sync",
        json={"venue": "crypto_com_exchange", "lookback_days": 90},
    ).json()
    assert second["results"][0]["detected"] == 1
    pending = client.get(
        "/native-flows", params={"venue": "crypto_com_exchange"}
    ).json()
    assert pending["venues"][0]["pending_count"] == 1
    assert pending["flows"][0]["source"] == "detected"


def test_sync_without_a_body_covers_every_account_venue(client, monkeypatch):
    for service in (exchange_service, revolut_service):
        monkeypatch.setattr(service, "status", lambda uid: {"connected": False})
    res = client.post("/native-flows/sync")
    assert {r["venue"] for r in res.json()["results"]} == {
        "crypto_com_exchange",
        "revolut_x",
        "trading212",
    }


def test_every_route_requires_the_signed_in_user():
    app = FastAPI()
    app.include_router(routes.router)
    # No override: the real dependency refuses an anonymous caller.
    anonymous = TestClient(app)
    assert anonymous.get("/native-flows").status_code in (401, 403)
    assert anonymous.post("/native-flows", json={}).status_code in (401, 403, 422)
