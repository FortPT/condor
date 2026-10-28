"""Kraken xStocks crew: discovery, actual MCP boundaries and authored review policy.

These tests do not run models or trade. Review is a prompt contract; money, pair and
session guards remain the native service's separately tested boundary
(tests/test_kraken_xstocks.py).
"""

import ast
import re
from pathlib import Path

import pytest

from condor.agents.agent import AgentStore
from condor.agents.strategy import StrategyStore
from condor.runtime.danger import is_dangerous_tool_call, is_mutating_tool_call
from condor.runtime.toolsets import allowlist_mutes, every_tool_name

ROOT = Path(__file__).resolve().parents[1]
COORDINATOR = "kraken_xstocks_trader"
ANALYST = "kraken_xstocks_analyst"
REVIEWER = "kraken_xstocks_risk_reviewer"
TOOLS = {
    COORDINATOR: {
        "kraken_xstocks",
        "delegate",
        "trading_agent_journal_read",
        "trading_agent_journal_write",
        "manage_memory",
        "send_notification",
    },
    # The analyst has no market tool: the coordinator passes it candle summaries.
    ANALYST: {"trading_agent_journal_read"},
    REVIEWER: {"trading_agent_journal_read"},
}


def flat(text: str) -> str:
    """Whitespace-normalized lower case, so a phrase may wrap across lines."""
    return " ".join(text.split()).lower()


def raw(text: str) -> str:
    return " ".join(text.split())


@pytest.fixture
def registry(monkeypatch, tmp_path):
    # Also safe with --noconftest in a minimal, isolated development environment.
    monkeypatch.setenv("CONDOR_AGENTS_ROOT", str(tmp_path / "agents"))
    monkeypatch.setenv("CONDOR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CONDOR_STOCK_AGENTS_ROOT", str(ROOT / "agents"))
    return AgentStore()


@pytest.mark.parametrize("slug", TOOLS)
def test_profiles_discoverable_with_enforced_exact_tool_boundaries(registry, slug):
    agent = registry.get(slug)
    assert agent is not None
    assert agent.agent_key == "claude-acp:sonnet"
    assert agent.server_required is False
    assert set(agent.tools) == TOOLS[slug]
    assert slug in registry.list_index()
    # Exercise the boundary used by Claude's MCP registration, not just YAML.
    assert every_tool_name() - allowlist_mutes(slug) == TOOLS[slug]
    assert "run_code" in allowlist_mutes(slug)
    assert "manage_agents" in allowlist_mutes(slug)
    assert "manage_agent_controllers" in allowlist_mutes(slug)
    # Neither the Crypto.com tool nor the paper test is reachable from this crew.
    assert "crypto_com_exchange" in allowlist_mutes(slug)
    assert "xstocks_paper" in allowlist_mutes(slug)


def test_only_the_analyst_and_reviewer_have_no_market_or_account_tool(registry):
    for slug in (ANALYST, REVIEWER):
        assert set(registry.get(slug).tools) == {"trading_agent_journal_read"}
        assert "kraken_xstocks" in allowlist_mutes(slug)


def test_only_coordinator_owns_a_loop_and_scheduler_defaults_survive(registry):
    loops = StrategyStore()
    assert loops.list(ANALYST) == []
    assert loops.list(REVIEWER) == []
    loop = loops.get(COORDINATOR, "intraday")
    assert loop is not None
    assert loop.default_config == {
        "frequency_sec": 300,
        "tick_timeout_sec": 600,
        "execution_mode": "loop",
        "total_amount_quote": 0,
        "restart_on_boot": True,
        "max_ticks": 0,
    }
    text = flat(loop.instructions)
    assert "explicit owner start" in text
    assert "native ledger" in text


def test_consultation_references_resolve_and_market_precedes_bounded_intent_review(
    registry,
):
    loop = StrategyStore().get(COORDINATOR, "intraday")
    for text in (registry.get(COORDINATOR).instructions, loop.instructions):
        text = raw(text)
        targets = re.findall(r'delegate\(action="ask", agent="([^"]+)"', text)
        assert targets == [ANALYST, REVIEWER]
        assert all(registry.get(slug) is not None for slug in targets)
        assert text.index(ANALYST) < text.index(REVIEWER)
        assert "sequential" in text
        assert "bounded" in text and "preview" in text
    text = raw(loop.instructions)
    assert (
        text.index('action="preview"')
        < text.index(f'agent="{REVIEWER}"')
        < text.index('action="order"')
    )
    # the candles are read before the analyst is asked: it has no market tool
    assert text.index('action="candles"') < text.index(f'agent="{ANALYST}"')


def test_authored_protocol_denies_missing_review_and_checks_every_final_bound(
    registry,
):
    coordinator = flat(registry.get(COORDINATOR).instructions)
    reviewer = flat(registry.get(REVIEWER).instructions)
    loop = flat(StrategyStore().get(COORDINATOR, "intraday").instructions)
    for text in (coordinator, loop):
        for condition in (
            "unavailable",
            "timeout",
            "missing answer",
            "reject",
            "incomplete",
        ):
            assert condition in text
        for guard in ("no order", "journal", "notify", "fresh preview", "new review"):
            assert guard in text
        assert "never enable" in text
        assert "native" in text and "guards" in text
    for field in (
        "preview",
        "side",
        "exact pair",
        "requested amount",
        "maximum amount",
        "maximum slippage percent",
        "costs/spread",
        "utc validity deadline",
        "allocation snapshot",
        "fresh preview",
    ):
        assert field in reviewer
    assert "unknown cost basis means incomplete, never approval" in reviewer
    assert "no quotation on kraken" in reviewer
    assert "out-of-bounds" in reviewer and "expired" in reviewer
    assert (
        loop.index(f'agent="{REVIEWER}"')
        < loop.index("take one fresh preview")
        < loop.index('action="order"')
    )
    for slug in TOOLS:
        instructions = flat(registry.get(slug).instructions)
        assert not re.search(r"eur (?:30|26|5)\b", instructions)
        assert "cumulative losses" in instructions


def test_the_round_trip_cost_comes_from_the_preview_and_is_never_hard_coded(registry):
    """The owner may be on a zero-fee plan: no fee, no 1.2% or 0.85% anywhere."""
    loop = StrategyStore().get(COORDINATOR, "intraday")
    for slug in TOOLS:
        text = flat(registry.get(slug).instructions)
        assert not re.search(r"\b(?:1\.2|0\.85|0\.4|0\.5|0\.26)\s?%", text), slug
    for text in (
        flat(registry.get(COORDINATOR).instructions),
        flat(loop.instructions),
    ):
        assert "estimated_round_trip_pct" in text or "preview's number" in text
        assert "zero-fee" in text
        assert "unknown" in text and "incomplete" in text
    assert "zero-fee" in flat(registry.get(REVIEWER).instructions)


def test_trading_style_matches_the_owners_aggressive_intraday_instruction(registry):
    loop = StrategyStore().get(COORDINATOR, "intraday")
    for text in (
        flat(registry.get(COORDINATOR).instructions),
        flat(loop.instructions),
    ):
        assert "1.5 times" in text
        assert "at least 1.0%" in text
        assert "1.5:1" in text
        assert "before the close" in text
        assert "100 round trips a month" in text
    coordinator = flat(registry.get(COORDINATOR).instructions)
    assert "15 to 25 percent of bot equity" in coordinator
    assert "5% below" in coordinator
    assert "review every held stock on every tick" in coordinator
    assert "an exit is a trade too" in coordinator
    reviewer = flat(registry.get(REVIEWER).instructions)
    assert "1.5 times the preview's estimated round-trip cost" in reviewer
    assert "at least 1.0%" in reviewer
    assert "closed session" in reviewer


def test_the_crew_trades_only_xstocks_in_usd_inside_the_regular_session(registry):
    loop = StrategyStore().get(COORDINATOR, "intraday")
    coordinator = flat(registry.get(COORDINATOR).instructions)
    text = flat(loop.instructions)
    assert "only tokenized us stocks" in coordinator
    assert "never trade another asset or pair" in coordinator
    assert "09:30 to 16:00 new york" in coordinator
    assert "usd" in coordinator
    # a closed session ends the tick before any scan, consultation or order
    first_step = text[text.index("1. call") : text.index("2. scan")]
    assert "closed" in first_step and "end the tick" in first_step
    assert "no scan, no analyst, no order" in first_step
    assert "only xstocks pairs in usd" in flat(registry.get(REVIEWER).instructions)


def test_nothing_in_the_crew_names_another_venues_tool(registry):
    for slug in TOOLS:
        text = registry.get(slug).instructions
        assert not re.search(r"\bcrypto_com_exchange\b", text), slug
        assert not re.search(r"\bxstocks_paper\b", text), slug
        assert not re.search(r"\b(?:get_prices|get_market_data|run_code)\b", text), slug
    loop = StrategyStore().get(COORDINATOR, "intraday").instructions
    assert not re.search(r"\b(?:crypto_com_exchange|xstocks_paper)\b", loop)


def _tool_node():
    tree = ast.parse(
        (ROOT / "mcp_servers/condor/server.py").read_text(encoding="utf-8")
    )
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "kraken_xstocks"
    )


def test_native_tool_exposes_no_enable_credentials_sync_or_money_limit_mutation():
    tool = _tool_node()
    actions = set(ast.literal_eval(tool.args.args[0].annotation.slice))
    assert actions == {
        "status",
        "valuation",
        "accounts",
        "history",
        "coins",
        "candles",
        "book",
        "preview",
        "order",
        "reconcile",
        "predict",
        "scorecard",
    }
    assert {arg.arg for arg in tool.args.args} == {
        "action",
        "side",
        "symbol",
        "amount",
        "max_slippage_pct",
        "timeframe",
        "count",
        "keyword",
        "verdict",
        "target_pct",
        "stop_pct",
        "horizon_hours",
        "note",
    }


@pytest.mark.parametrize("name", ["kraken_xstocks", "mcp__condor__kraken_xstocks"])
def test_only_order_is_dangerous_and_the_reads_are_read_only(name):
    def call(action):
        return {"tool": name, "input": {"action": action}}

    assert is_dangerous_tool_call(call("order"))
    assert is_mutating_tool_call(call("order"))
    for action in (
        "status",
        "valuation",
        "accounts",
        "history",
        "coins",
        "candles",
        "book",
        "preview",
        "predict",
        "scorecard",
    ):
        assert not is_dangerous_tool_call(call(action)), action
        assert not is_mutating_tool_call(call(action)), action
    # reconcile settles the ledger: it is a write for the log, never a read
    assert is_mutating_tool_call(call("reconcile"))
    # unreadable arguments fail closed
    assert is_dangerous_tool_call({"tool": name, "input": "not json"})
