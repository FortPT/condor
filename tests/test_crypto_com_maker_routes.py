"""The maker arguments and read-only stats through the web route and the MCP tool.

No exchange is contacted: the arguments are validated before any network call, and
the MCP tool is checked against a recording stand-in for the Condor API.
"""

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from condor import crypto_com_exchange as ex
from condor.web.auth import get_current_user
from condor.web.models import WebUser
from condor.web.routes import crypto_com_exchange as routes


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDOR_DATA_DIR", str(tmp_path))


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_current_user] = lambda: WebUser(
        id=7, username="u", first_name="U", role="user"
    )
    return TestClient(app)


ORDER = {"side": "sell", "symbol": "SHIB", "amount": "1000"}


def test_maker_stats_are_read_only_and_empty_at_first(client):
    body = client.get("/crypto-exchange/maker-stats").json()
    assert body["maker_enabled"] is False
    assert body["maker_fee_overrides"] == {}
    assert body["stats"]["attempts"] == 0
    assert body["stats"]["fill_rate"] is None


def test_the_owner_switch_turns_maker_mode_on_and_off(client):
    on = client.put("/crypto-exchange/maker-mode", json={"enabled": True}).json()
    assert on["maker_enabled"] is True
    assert client.get("/crypto-exchange/maker-stats").json()["maker_enabled"] is True
    off = client.put("/crypto-exchange/maker-mode", json={"enabled": False}).json()
    assert off["maker_enabled"] is False


def test_the_stats_reflect_the_log(client):
    ex._write(
        ex._maker_log_path(7),
        {
            "attempts": [
                {"outcome": "filled", "purpose": "entry", "value": "10"},
                {"outcome": "unfilled", "purpose": "entry"},
            ]
        },
    )
    stats = client.get("/crypto-exchange/maker-stats").json()["stats"]
    assert (stats["attempts"], stats["fill_rate"]) == (2, 0.5)


@pytest.mark.parametrize("path", ["preview", "order"])
def test_a_maker_stop_exit_is_refused_by_the_route(client, path):
    res = client.post(
        f"/crypto-exchange/{path}",
        json={**ORDER, "mode": "maker", "purpose": "stop_exit"},
    )
    assert res.status_code == 400
    assert "stop" in res.json()["detail"]
    assert "taker" in res.json()["detail"]


@pytest.mark.parametrize("path", ["preview", "order"])
def test_a_maker_order_needs_a_purpose_and_a_sane_wait(client, path):
    missing = client.post(f"/crypto-exchange/{path}", json={**ORDER, "mode": "maker"})
    assert missing.status_code == 400
    too_long = client.post(
        f"/crypto-exchange/{path}",
        json={
            **ORDER,
            "mode": "maker",
            "purpose": "target_exit",
            "maker_wait_sec": "90",
        },
    )
    assert too_long.status_code == 400
    assert "75" in too_long.json()["detail"]


def test_the_request_defaults_keep_the_taker_path():
    body = routes.OrderRequest(side="buy", symbol="ETH", amount="5")
    assert (body.mode, body.purpose, body.maker_wait_sec) == ("taker", "", "")


@pytest.mark.asyncio
async def test_the_mcp_tool_forwards_the_maker_arguments(monkeypatch):
    from mcp_servers.condor import condor_client, server

    calls = []

    async def fake(method, path, body=None, **kwargs):
        calls.append((method, path, body, kwargs))
        return {"ok": True}

    monkeypatch.setattr(condor_client, "call_main_api", fake)
    await server.crypto_com_exchange(
        action="order",
        side="buy",
        symbol="ETH",
        amount="8",
        mode="maker",
        purpose="entry",
        maker_wait_sec="45",
    )
    method, path, body, kwargs = calls[-1]
    assert (method, path) == ("POST", "/crypto-exchange/order")
    assert (body["mode"], body["purpose"], body["maker_wait_sec"]) == (
        "maker",
        "entry",
        "45",
    )
    # the tool call must outlast the maker wait plus cancel and final read
    assert kwargs["timeout"] == server.CRYPTO_COM_ORDER_TIMEOUT
    assert ex.MAKER_CALL_BUDGET_SEC + 12 <= server.CRYPTO_COM_ORDER_TIMEOUT
    assert ex.MAKER_WAIT_MAX_SEC < server.CRYPTO_COM_ORDER_TIMEOUT

    await server.crypto_com_exchange(
        action="order", side="buy", symbol="ETH", amount="8"
    )
    assert calls[-1][2]["mode"] == "taker"
    assert calls[-1][2]["purpose"] == ""

    await server.crypto_com_exchange(
        action="preview", side="sell", symbol="ETH", amount="1"
    )
    assert calls[-1][1] == "/crypto-exchange/preview"
    assert calls[-1][2]["mode"] == "taker"

    result = await server.crypto_com_exchange(action="maker_stats")
    assert calls[-1][:2] == ("GET", "/crypto-exchange/maker-stats")
    assert result == {"data": {"ok": True}}


def test_maker_stats_is_not_an_order_for_the_danger_gate():
    from condor.runtime.danger import _has_dangerous_action

    stats = {
        "tool": "mcp__condor__crypto_com_exchange",
        "input": {"action": "maker_stats"},
    }
    order = {
        "tool": "mcp__condor__crypto_com_exchange",
        "input": {"action": "order", "mode": "maker", "purpose": "entry"},
    }
    assert not _has_dangerous_action(stats, {"order"})
    assert _has_dangerous_action(order, {"order"})
