"""``GET /servers/{name}/dex/swaps``: the swap table of the Solana Trade tab."""

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import condor.web.routes.dex as dex_routes
from condor.web.auth import get_current_user
from condor.web.models import WebUser

USER = WebUser(id=111, username="u", first_name="U", role="user")

ROW = {
    "status": "CONFIRMED",
    "side": "SELL",
    "trading_pair": "SOL-USDC",
    "connector": "jupiter/router",
    "network": "solana-mainnet-beta",
    "input_amount": 0.05,
    "output_amount": 4.7,
    "price": 94.0,
    "base_token": "SOL",
    "quote_token": "USDC",
    "transaction_hash": "5abc",
    "timestamp": "2026-11-01T00:10:00Z",
    "wallet_address": "85maohASGbekdraAsBkX9XLXppwmFDpF9nnboKPCSqbn",
    "signature_blob": "must not leave the server",
}


class FakeSwaps:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, []

    async def search_swaps(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.result


class FakeClient:
    def __init__(self, swaps):
        self.gateway_swap = swaps


class FakeConfigManager:
    def __init__(self, client):
        self._client = client

    def has_server_access(self, user_id, name, *a, **kw):
        return True

    async def get_client(self, name):
        return self._client


@pytest.fixture
def route_client(monkeypatch):
    def build(client):
        cm = FakeConfigManager(client)
        monkeypatch.setattr(dex_routes, "get_config_manager", lambda: cm)
        monkeypatch.setattr("condor.web.auth.get_config_manager", lambda: cm)
        app = FastAPI()
        app.include_router(dex_routes.router)
        app.dependency_overrides[get_current_user] = lambda: USER
        return TestClient(app)

    return build


def test_swaps_come_back_reduced_to_the_table_fields(route_client):
    swaps = FakeSwaps({"data": [ROW]})
    client = route_client(FakeClient(swaps))
    body = client.get("/servers/srv/dex/swaps").json()
    assert body["swaps"] == [{key: ROW[key] for key in dex_routes._SWAP_KEYS}]
    assert "wallet_address" not in body["swaps"][0]
    assert "signature_blob" not in body["swaps"][0]
    assert swaps.calls == [{"limit": 25}]


def test_the_limit_is_passed_through_and_bounded(route_client):
    swaps = FakeSwaps({"data": []})
    client = route_client(FakeClient(swaps))
    assert client.get("/servers/srv/dex/swaps?limit=5").json() == {"swaps": []}
    assert swaps.calls == [{"limit": 5}]
    assert client.get("/servers/srv/dex/swaps?limit=0").status_code == 422
    assert client.get("/servers/srv/dex/swaps?limit=101").status_code == 422


def test_a_bare_list_and_junk_rows_are_tolerated(route_client):
    client = route_client(FakeClient(FakeSwaps([ROW, "junk", None])))
    assert len(client.get("/servers/srv/dex/swaps").json()["swaps"]) == 1


def test_an_unreachable_server_is_an_error_not_an_empty_list(route_client):
    client = route_client(FakeClient(FakeSwaps(error=RuntimeError("gateway down"))))
    r = client.get("/servers/srv/dex/swaps")
    assert r.status_code == 502
    assert "gateway down" not in r.text or "Failed to list swaps" in r.text


def test_a_server_without_gateway_swap_says_so(route_client):
    class NoSwaps:
        pass

    r = route_client(NoSwaps()).get("/servers/srv/dex/swaps")
    assert r.status_code == 501
