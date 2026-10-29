"""The paper xStocks crew: one agent, one loop, a simulated account, the live crew's rules.

These tests do not run models or trade. They pin the authored policy (the paper twin must
decide like the live Kraken crew, so the comparison is fair) and the tool boundary. The
ledger's own guards are tested in tests/test_xstocks_paper_ledger.py.
"""

import re
from pathlib import Path

import pytest

from condor.agents.agent import AgentStore
from condor.agents.strategy import StrategyStore
from condor.runtime.toolsets import allowlist_mutes, every_tool_name

ROOT = Path(__file__).resolve().parents[1]
SLUG = "xstocks_paper_trader"
LIVE = "kraken_xstocks_trader"
TOOLS = {
    "xstocks_paper",
    "trading_agent_journal_read",
    "trading_agent_journal_write",
    "manage_memory",
    "send_notification",
}


def flat(text: str) -> str:
    return " ".join(text.split()).lower()


@pytest.fixture
def registry(monkeypatch, tmp_path):
    monkeypatch.setenv("CONDOR_AGENTS_ROOT", str(tmp_path / "agents"))
    monkeypatch.setenv("CONDOR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CONDOR_STOCK_AGENTS_ROOT", str(ROOT / "agents"))
    return AgentStore()


def test_the_paper_trader_has_exactly_its_tools_and_no_live_ones(registry):
    agent = registry.get(SLUG)
    assert agent is not None and set(agent.tools) == TOOLS
    assert every_tool_name() - allowlist_mutes(SLUG) == TOOLS
    for live in ("kraken_xstocks", "crypto_com_exchange", "revolut_x", "delegate"):
        assert live in allowlist_mutes(SLUG)
    assert "run_code" in allowlist_mutes(SLUG)


def test_the_loop_keeps_the_live_crews_cadence_and_never_owns_capital(registry):
    loop = StrategyStore().get(SLUG, "paper_session")
    assert loop is not None
    assert loop.default_config == {
        "frequency_sec": 300,
        "tick_timeout_sec": 420,
        "execution_mode": "loop",
        "total_amount_quote": 0,
        "restart_on_boot": True,
        "max_ticks": 0,
    }
    text = flat(loop.instructions)
    assert "explicit owner start" in text
    assert "paper ledger owns capital" in text
    assert "simulated" in text


def test_the_loop_previews_then_orders_once_with_a_client_id(registry):
    text = " ".join(StrategyStore().get(SLUG, "paper_session").instructions.split())
    steps = [
        text.index('action="status"'),
        text.index('action="valuation"'),
        text.index('action="candles"'),
        text.index('action="predict"'),
        text.index('action="preview"'),
        text.index('action="order"'),
    ]
    assert steps == sorted(steps)
    assert 'client_id="<NY date>-<tick>-<TICKER>-<side>"' in text
    # there is no reviewer and no delegation on paper
    assert "delegate" not in text
    assert "There is no reviewer on paper" in text
    assert not re.search(r'action="reset"', text)


def test_the_paper_twin_applies_the_live_crews_decision_rules(registry):
    live = StrategyStore().get(LIVE, "intraday")
    paper = StrategyStore().get(SLUG, "paper_session")
    texts = {
        "live": flat(registry.get(LIVE).instructions + live.instructions),
        "paper": flat(registry.get(SLUG).instructions + paper.instructions),
    }
    # The numbers that decide a trade must be the same on both sides.
    for rule in (
        "at least 1.5 times",
        "at least 1.0%",
        "no farther than two thirds of the target",
        "15 to 25 percent",
        "never below 5 usd",
        "2 equity",
        "5% below",
        "40% asset cap",
        "one sixth",
        "at most one new entry per tick",
        "about 100 round trips a month",
        "thesis is stale",
    ):
        assert rule in texts["paper"], rule
    for rule in ("1.5 times", "1.0%", "two thirds of the target", "15 to 25 percent"):
        assert rule in texts["live"] and rule in texts["paper"], rule


def test_the_cost_comes_from_the_preview_and_the_reasoning_is_journaled(registry):
    agent = flat(registry.get(SLUG).instructions)
    loop = flat(StrategyStore().get(SLUG, "paper_session").instructions)
    assert "never hard-code the cost" in loop
    assert "take the round trip from the preview" in agent
    assert "journal the reasoning and the bounds" in agent
    assert "you must journal the reasoning and the bounds before every order" in agent
    assert "you can never reset it yourself" in agent
    assert "nothing you do here can move money" in agent


def test_the_prompts_never_present_the_account_as_real_money(registry):
    agent = registry.get(SLUG).instructions
    assert "simulated" in agent.lower()
    assert "no exchange account" in flat(agent)
