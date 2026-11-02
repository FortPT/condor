"""The daily SOL signal for the Solana trend crew: plan, wallet balances, route and tool."""

import inspect
import time
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from starlette.testclient import TestClient

import condor.fetchers.market_data as market_data
import condor.web.routes.dex as dex_routes
from condor import solana_wallet
from condor import trend_signal as ts
from condor.web.auth import get_current_user
from condor.web.models import WebUser

USER = WebUser(id=111, username="u", first_name="U", role="user")
AGENTS = Path(__file__).resolve().parent.parent / "agents"
DAY = 86_400


@pytest.mark.parametrize(
    ("signal", "sol", "usdc", "action"),
    [
        ("flat", 0.3294, 0.0, "sell_sol"),
        ("flat", 0.03, 0.0, "hold"),  # 0.01 SOL above the reserve is under 3 USD
        ("flat", 0.0, 25.0, "hold"),
        ("long", 0.02, 20.0, "buy_sol"),
        ("long", 0.3, 2.0, "hold"),
        ("no_data", 0.3, 20.0, "none"),
    ],
)
def test_the_plan_follows_the_signal_and_leaves_the_fee_reserve(
    signal, sol, usdc, action
):
    plan = ts.solana_plan(signal, sol, usdc, 94.0)
    assert plan["action"] == action
    if action == "sell_sol":
        assert plan["sell_sol_units"] == pytest.approx(sol - ts.SOL_RESERVE, abs=1e-6)
    if action == "buy_sol":
        assert plan["buy_usdc"] == usdc


def test_a_missing_price_never_plans_a_sale():
    assert ts.solana_plan("flat", 1.0, 0.0, None)["action"] == "hold"


def test_hummingbot_candles_become_the_rows_the_signal_reads():
    rows = ts.from_normalized(
        [
            {"timestamp": 1.0, "open": 2, "high": 4, "low": 1, "close": 3, "volume": 9},
            "junk",
        ]
    )
    assert rows == [{"t": 1.0, "o": 2, "h": 4, "l": 1, "c": 3}]
    assert ts.from_normalized(None) == []


def test_balances_read_sol_and_usdc_from_the_solana_connector_only():
    state = {
        "master": {
            "solana-mainnet-beta": [
                {"token": "SOL", "units": 0.3, "value": 28.0},
                {"token": "USDC", "units": 5.0, "value": 5.0},
                {"token": "JUP", "units": 9.0, "value": 2.0},
            ],
            "binance": [{"token": "SOL", "units": 99, "value": 9999}],
        }
    }
    assert solana_wallet.balances_from_portfolio(state) == {
        "SOL": {"units": 0.3, "value": 28.0},
        "USDC": {"units": 5.0, "value": 5.0},
    }
    assert solana_wallet.balances_from_portfolio(None) == {}


def candles(closes):
    """Daily candles ending yesterday (UTC), as the Hummingbot client normalizes them."""
    end = int(time.time() // DAY) * DAY - DAY
    start = end - (len(closes) - 1) * DAY
    return [
        {
            "timestamp": float(start + i * DAY),
            "open": c,
            "high": c,
            "low": c,
            "close": c,
            "volume": 1.0,
        }
        for i, c in enumerate(closes)
    ]


FALLING = candles(
    [100.0 + i for i in range(140)] + [240.0 - 4 * k for k in range(1, 41)]
)
RISING = candles([100.0 + i for i in range(180)])


class FakePortfolio:
    def __init__(self, state):
        self.state = state

    async def get_state(self):
        return self.state


class FakeClient:
    def __init__(self, state):
        self.portfolio = FakePortfolio(state)


class FakeConfigManager:
    def __init__(self, client, default="srv"):
        self._client, self._default = client, default

    def get_default_server(self):
        return self._default

    def has_server_access(self, user_id, name, *a, **kw):
        return True

    async def get_client(self, name):
        return self._client


WALLET = {
    "master": {
        "solana-mainnet-beta": [{"token": "SOL", "units": 0.3294, "value": 31.0}]
    }
}


@pytest.fixture
def route(monkeypatch):
    def build(state=WALLET, rows=FALLING, default="srv", error=None):
        cm = FakeConfigManager(FakeClient(state), default)
        monkeypatch.setattr(dex_routes, "get_config_manager", lambda: cm)
        monkeypatch.setattr("condor.web.auth.get_config_manager", lambda: cm)

        async def fake_candles(client, connector, pair, interval, **kwargs):
            assert (connector, pair, interval) == ("binance", "SOL-USDT", "1d")
            if error:
                raise error
            return rows

        monkeypatch.setattr(market_data, "fetch_historical_candles", fake_candles)
        app = FastAPI()
        app.include_router(dex_routes.router)
        app.dependency_overrides[get_current_user] = lambda: USER
        return TestClient(app)

    return build


def test_a_flat_signal_plans_the_sale_of_the_sol_above_the_reserve(route):
    body = route().get("/solana/trend").json()
    assert body["coin"]["signal"] == "flat"
    assert body["plan"]["action"] == "sell_sol"
    assert body["plan"]["sell_sol_units"] == pytest.approx(0.3294 - 0.02, abs=1e-6)
    assert body["wallet"]["sol_units"] == 0.3294
    assert body["wallet"]["sol_price_usd"] == pytest.approx(31.0 / 0.3294, abs=1e-3)
    assert "address" not in body["wallet"] and "places nothing" in body["note"]


def test_a_long_signal_with_usdc_plans_the_purchase(route):
    state = {
        "master": {
            "solana-mainnet-beta": [
                {"token": "SOL", "units": 0.02, "value": 1.9},
                {"token": "USDC", "units": 20.0, "value": 20.0},
            ]
        }
    }
    body = route(state, RISING).get("/solana/trend").json()
    assert body["coin"]["signal"] == "long"
    assert body["plan"] == {
        "action": "buy_sol",
        "sell_sol_units": 0.0,
        "buy_usdc": 20.0,
        "tradable_sol": 0.0,
        "reserve_sol": 0.02,
    }


def test_too_few_candles_is_no_data_and_no_plan(route):
    body = route(WALLET, FALLING[-30:]).get("/solana/trend").json()
    assert body["coin"]["signal"] == "no_data"
    assert body["plan"]["action"] == "none"


def test_no_default_server_and_a_failing_read_are_errors_not_plans(route):
    assert route(default=None).get("/solana/trend").status_code == 409
    assert route(error=RuntimeError("down")).get("/solana/trend").status_code == 502


def test_the_tool_takes_no_arguments():
    from mcp_servers.condor import server

    assert list(inspect.signature(server.solana_trend).parameters) == []


def test_the_solana_crew_follows_the_code_signal_inside_the_swap_limits():
    text = (AGENTS / "solana_trend_trader" / "AGENT.md").read_text(encoding="utf-8")
    head = yaml.safe_load(text.split("---")[1])
    body = " ".join(text.split("---", 2)[2].split())
    assert set(head["tools"]) == {
        "solana_trend",
        "quote_swap",
        "execute_swap",
        "get_swap_status",
        "resolve_swap",
        "trading_agent_journal_read",
        "trading_agent_journal_write",
        "send_notification",
        "manage_memory",
    }
    assert "Act on the `plan.action` and nothing else" in body
    assert "At most ONE swap per tick" in body and "12 USDC" in body
    assert "never retry a swap" in body
    assert "SOL and USDC only" in body and "Never use another wallet" in body

    loop = (AGENTS / "solana_trend_trader" / "loops" / "daily" / "loop.md").read_text(
        encoding="utf-8"
    )
    cfg = yaml.safe_load(loop.split("---")[1])["default_config"]
    assert cfg["frequency_sec"] == 3600 and cfg["execution_mode"] == "loop"
    limits = cfg["risk_limits"]
    assert limits["allowed_swap_tokens"] == "SOL,USDC"
    assert limits["max_swap_quote"] == 12 and limits["min_swap_sol_reserve"] == 0.02
    assert limits["swap_default_wallet_only"] is True
    assert "starts nothing and changes no financial activation" in loop
