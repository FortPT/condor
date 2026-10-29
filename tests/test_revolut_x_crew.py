"""Revolut X crew: discovery, actual MCP boundaries and authored review policy.

These tests do not run models or trade. Review is a prompt contract; money, pair and
key guards remain the native service's separately tested boundary
(tests/test_revolut_x.py).
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
COORDINATOR = "revolut_x_trader"
ANALYST = "revolut_x_analyst"
REVIEWER = "revolut_x_risk_reviewer"
TOOLS = {
    COORDINATOR: {
        "revolut_x",
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
    # No other venue's tool is reachable from this crew.
    assert "crypto_com_exchange" in allowlist_mutes(slug)


def test_only_the_analyst_and_reviewer_have_no_market_or_account_tool(registry):
    for slug in (ANALYST, REVIEWER):
        assert set(registry.get(slug).tools) == {"trading_agent_journal_read"}
        assert "revolut_x" in allowlist_mutes(slug)


def test_no_other_crew_can_reach_the_revolut_x_tool(registry):
    for slug in ("crypto_com_exchange_trader",):
        assert "revolut_x" in allowlist_mutes(slug), slug


def test_only_coordinator_owns_a_loop_and_scheduler_defaults_survive(registry):
    loops = StrategyStore()
    assert loops.list(ANALYST) == []
    assert loops.list(REVIEWER) == []
    loop = loops.get(COORDINATOR, "intraday")
    assert loop is not None
    assert loop.default_config == {
        "frequency_sec": 120,
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
    assert "no quotation on revolut x" in reviewer
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
    """No fee endpoint exists: no fee or cost figure may be written into a prompt."""
    loop = StrategyStore().get(COORDINATOR, "intraday")
    for slug in TOOLS:
        text = flat(registry.get(slug).instructions)
        assert not re.search(
            r"\b(?:1\.6|1\.2|0\.85|0\.4|0\.5|0\.26|0\.09|0\.35|0\.18)\s?%", text
        ), slug
    for text in (
        flat(registry.get(COORDINATOR).instructions),
        flat(loop.instructions),
    ):
        assert "estimated_round_trip_pct" in text
        assert "never hard-code the fee" in text or "never assumed" in text
        assert "estimate" in text
    reviewer = flat(registry.get(REVIEWER).instructions)
    assert "publishes no fee endpoint" in reviewer
    assert "estimate" in reviewer and "unknown" in reviewer


def test_trading_style_matches_the_owners_intraday_instruction_with_a_derived_bar(
    registry,
):
    loop = StrategyStore().get(COORDINATOR, "intraday")
    for text in (
        flat(registry.get(COORDINATOR).instructions),
        flat(loop.instructions),
    ):
        assert "1.5 times" in text
        assert "at least 0.8%" in text
        assert "1.5:1" in text
        assert "1 to 24 hours" in text
        assert "0.15 percentage points" in text
    coordinator = flat(registry.get(COORDINATOR).instructions)
    assert "15 to 25 percent of bot equity" in coordinator
    assert "review every held coin on every tick" in coordinator
    assert "an exit is a trade too" in coordinator
    reviewer = flat(registry.get(REVIEWER).instructions)
    assert "1.5 times the preview's estimated round-trip cost" in reviewer
    assert "at least 0.8%" in reviewer
    assert "0.15 percentage points above the preview's round-trip cost" in reviewer
    analyst = flat(registry.get(ANALYST).instructions)
    assert "at least 0.8%" in analyst and "1.5:1" in analyst


def test_the_crew_sizes_entries_against_cent_rounding_and_skips_a_tiny_account(
    registry,
):
    loop = StrategyStore().get(COORDINATOR, "intraday")
    coordinator = flat(registry.get(COORDINATOR).instructions)
    text = flat(loop.instructions)
    for prompt in (coordinator, text):
        assert "rounds eur amounts and fees to the cent" in prompt
        assert "rounding_allowance_pct" in prompt
        assert "at least 10 eur" in prompt
        assert "cost_guard_blocks_buy" in prompt
        assert "under about 25 eur of bot equity" in prompt
        assert "not worth day-trading" in prompt
        assert "exits" in prompt
    assert "too expensive" in coordinator and "pass" in coordinator
    assert "never try to get around that refusal" in coordinator
    reviewer = flat(registry.get(REVIEWER).instructions)
    assert "rounding_allowance_pct" in reviewer
    assert "never rejected for being small" in reviewer
    analyst = flat(registry.get(ANALYST).instructions)
    assert "cent-rounding allowance" in analyst
    # the rounding figure comes from the preview too: no number is hard-coded
    for slug in TOOLS:
        assert "1%" not in flat(registry.get(slug).instructions)


def test_the_daily_brake_is_five_percent_below_the_utc_day_opening_equity(registry):
    loop = StrategyStore().get(COORDINATOR, "intraday")
    for text in (
        flat(registry.get(COORDINATOR).instructions),
        flat(loop.instructions),
    ):
        assert "5% below" in text
        assert "utc day" in text
        assert "00:00 utc" in text
        assert "opening equity" in text
        assert "exits stay allowed" in text


def test_exits_are_never_blocked_on_incomplete_paperwork(registry):
    loop = StrategyStore().get(COORDINATOR, "intraday")
    coordinator = flat(registry.get(COORDINATOR).instructions)
    text = flat(loop.instructions)
    assert "exits are never blocked on paperwork" in coordinator
    assert "if the reviewer returns incomplete twice" in coordinator
    assert "escalate by re-asking with the missing bounds filled in" in coordinator
    assert "only an approve" in text or "never place an exit without an approve" in (
        coordinator
    )
    assert "incomplete twice escalate by re-asking" in text
    reviewer = flat(registry.get(REVIEWER).instructions)
    assert "exits are never left waiting on paperwork" in reviewer
    assert "say exactly which bound is missing" in reviewer
    # an exit still needs an approve: the stop is never placed on a reject
    assert "a reject, or any answer that is not an approve, still means no order" in (
        coordinator
    )


def test_a_fresh_preview_inside_the_cost_bound_places_the_order(registry):
    coordinator = flat(registry.get(COORDINATOR).instructions)
    loop = flat(StrategyStore().get(COORDINATOR, "intraday").instructions)
    for text in (coordinator, loop):
        assert "inside the approved bound" in text or "inside the bound" in text
        assert "changed pair, side or amount" in text


def test_the_crew_trades_only_euro_pairs_around_the_clock(registry):
    loop = StrategyStore().get(COORDINATOR, "intraday")
    coordinator = flat(registry.get(COORDINATOR).instructions)
    text = flat(loop.instructions)
    assert "only crypto pairs quoted in eur" in coordinator
    assert "never trade another quote currency or pair" in coordinator
    assert "never treat usd, usdt, usdc or gbp as eur" in coordinator
    assert "around the clock" in coordinator
    for slug in TOOLS:
        instructions = flat(registry.get(slug).instructions)
        assert "regular us session" not in instructions
        assert "09:30" not in instructions
        assert "close of the session" not in instructions
    assert "regular us session" not in text and "before the close" not in text
    assert "eur pairs only" in text
    assert "do not treat usd, usdt, usdc or gbp as eur" in flat(
        registry.get(REVIEWER).instructions
    )
    assert "never equate usd, usdt or gbp with eur" in flat(
        registry.get(ANALYST).instructions
    )


def test_nothing_in_the_crew_names_another_venues_tool(registry):
    for slug in TOOLS:
        text = registry.get(slug).instructions
        assert not re.search(r"\bcrypto_com_exchange\b", text), slug
        assert not re.search(r"\b(?:get_prices|get_market_data|run_code)\b", text), slug
    loop = StrategyStore().get(COORDINATOR, "intraday").instructions
    assert not re.search(r"\bcrypto_com_exchange\b", loop)


def test_the_crew_never_touches_keys_or_credentials(registry):
    for slug in TOOLS:
        text = flat(registry.get(slug).instructions)
        assert "credentials" in text
    coordinator = flat(registry.get(COORDINATOR).instructions)
    assert "change the signing key or api key" in coordinator
    assert "never send credentials" in coordinator


def _tool_node():
    tree = ast.parse(
        (ROOT / "mcp_servers/condor/server.py").read_text(encoding="utf-8")
    )
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "revolut_x"
    )


def test_native_tool_exposes_no_key_credentials_sync_enable_or_limit_mutation():
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


def test_the_cost_guard_override_is_the_owners_api_only_never_the_crews_tool():
    server = (ROOT / "mcp_servers/condor/server.py").read_text(encoding="utf-8")
    assert "allow_costly_buy" not in server
    route = (ROOT / "condor/web/routes/revolut_x.py").read_text(encoding="utf-8")
    assert "allow_costly_buy" in route


@pytest.mark.parametrize("name", ["revolut_x", "mcp__condor__revolut_x"])
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
    # an action this module has not heard of fails open as a write
    assert is_mutating_tool_call(call("generate_signing_key"))
    # unreadable arguments fail closed
    assert is_dangerous_tool_call({"tool": name, "input": "not json"})
