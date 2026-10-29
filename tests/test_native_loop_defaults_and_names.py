"""The shipped native loops carry the cadence the owner runs, and users see "Crypto.com".

A restart on boot rebuilds a loop's config from the ``default_config`` in its ``loop.md``
(overlaid with whatever was explicitly saved), so the repository is where the cadence has
to be right. The Crypto.com loop had come back at 300 s instead of 120 s.
"""

import asyncio
from pathlib import Path

import pytest

from condor.agents.agent import AgentStore
from condor.agents.strategy import StrategyStore

ROOT = Path(__file__).resolve().parents[1]

CADENCE = {
    ("crypto_com_exchange_trader", "daily_spot"): (120, 600),
    ("revolut_x_trader", "intraday"): (120, 600),
    ("kraken_xstocks_trader", "intraday"): (300, 600),
}


@pytest.fixture
def registry(monkeypatch, tmp_path):
    monkeypatch.setenv("CONDOR_AGENTS_ROOT", str(tmp_path / "agents"))
    monkeypatch.setenv("CONDOR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CONDOR_STOCK_AGENTS_ROOT", str(ROOT / "agents"))
    return AgentStore()


@pytest.mark.parametrize(("agent", "loop"), sorted(CADENCE))
def test_each_native_loop_ships_the_cadence_in_use(registry, agent, loop):
    strategy = StrategyStore().get(agent, loop)
    assert strategy is not None
    frequency, timeout = CADENCE[(agent, loop)]
    assert strategy.default_config["frequency_sec"] == frequency
    assert strategy.default_config["tick_timeout_sec"] == timeout
    assert strategy.default_config["restart_on_boot"] is True


def test_the_crypto_com_agents_are_named_crypto_com_for_the_user(registry):
    names = {
        slug: registry.get(slug)
        for slug in (
            "crypto_com_exchange_trader",
            "crypto_com_market_analyst",
            "crypto_com_risk_reviewer",
        )
    }
    assert names["crypto_com_exchange_trader"].name == "Crypto.com Trader"
    for slug, agent in names.items():
        assert agent is not None
        assert "Exchange" not in agent.name, slug
        assert "Exchange" not in (agent.description or ""), slug
    loop = StrategyStore().get("crypto_com_exchange_trader", "daily_spot")
    assert "Exchange" not in (loop.description or "")


def _surface():
    from mcp_servers.condor import server as condor_server
    from mcp_servers.condor.profiles import TOOL_DESCRIPTIONS

    probe = type(condor_server.mcp)("naming-probe")
    condor_server.register_tools(probe, "full")
    tools = asyncio.run(probe.list_tools())
    return {t.name: t.description or "" for t in tools}, TOOL_DESCRIPTIONS


def test_tool_descriptions_name_the_venue_crypto_com_and_do_not_call_the_scorecard_paper():
    described, short = _surface()
    for name in (
        "crypto_com_exchange",
        "kraken_xstocks",
        "revolut_x",
        "native_portfolio",
    ):
        text = described[name]
        assert "Crypto.com Exchange" not in text, name
        assert "paper" not in text.lower(), name
    for name in ("crypto_com_exchange", "kraken_xstocks", "revolut_x"):
        assert "scores the calls, not the trades actually placed" in described[name]
    assert "Crypto.com" in short["crypto_com_exchange"]
    assert "Exchange" not in short["crypto_com_exchange"]
    assert "Crypto.com Exchange" not in short["native_portfolio"]
