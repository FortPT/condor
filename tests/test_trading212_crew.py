"""Trading 212 crew: discovery, actual MCP boundaries and authored review policy.

These tests do not run models or trade. Review is a prompt contract; money, ticker,
currency and session guards remain the native service's separately tested boundary
(tests/test_trading212.py).
"""

import ast
import re
from pathlib import Path

import pytest

from condor import native_venues
from condor.agents.agent import AgentStore
from condor.agents.strategy import StrategyStore
from condor.runtime.danger import is_dangerous_tool_call, is_mutating_tool_call
from condor.runtime.toolsets import allowlist_mutes, every_tool_name

ROOT = Path(__file__).resolve().parents[1]
COORDINATOR = "trading212_trader"
ANALYST = "trading212_analyst"
REVIEWER = "trading212_risk_reviewer"
TOOLS = {
    COORDINATOR: {
        "trading212",
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
    assert "revolut_x" in allowlist_mutes(slug)


def test_only_the_analyst_and_reviewer_have_no_market_or_account_tool(registry):
    for slug in (ANALYST, REVIEWER):
        assert set(registry.get(slug).tools) == {"trading_agent_journal_read"}
        assert "trading212" in allowlist_mutes(slug)


def test_no_other_crew_can_reach_the_trading212_tool(registry):
    for slug in (
        "crypto_com_exchange_trader",
        "revolut_x_trader",
    ):
        assert "trading212" in allowlist_mutes(slug), slug


def test_the_crew_is_registered_as_the_trading212_native_venue():
    venue = native_venues.for_agent(COORDINATOR)
    assert venue is not None and venue.key == "trading212"
    assert venue.label == "Trading 212"
    assert venue.agents == (COORDINATOR,)
    # the analyst and the reviewer hold no money: only the coordinator has a loop
    assert native_venues.for_agent(ANALYST) is None
    assert native_venues.for_agent(REVIEWER) is None


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
    assert "no quotation on trading 212" in reviewer
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
    """No fee or cost figure is written into a prompt, not even the published FX fee."""
    loop = StrategyStore().get(COORDINATOR, "intraday")
    for slug in TOOLS:
        text = flat(registry.get(slug).instructions)
        assert not re.search(
            r"\b(?:1\.6|1\.2|0\.85|0\.45|0\.4|0\.5|0\.26|0\.3|0\.15|0\.09|0\.35)\s?%",
            text,
        ), slug
    for text in (
        flat(registry.get(COORDINATOR).instructions),
        flat(loop.instructions),
    ):
        assert "estimated_round_trip_pct" in text
        assert "never hard-code the fee" in text or "never assumed" in text
        assert "estimate" in text
    reviewer = flat(registry.get(REVIEWER).instructions)
    assert "supported cost basis" in reviewer
    assert "estimate" in reviewer and "unknown" in reviewer
    assert "published" in reviewer


def test_no_balance_or_amount_is_written_into_a_prompt(registry):
    """Capital is read from the native ledger: the only EUR figure is the entry size."""
    loop = StrategyStore().get(COORDINATOR, "intraday")
    for text in [loop.instructions] + [
        registry.get(slug).instructions for slug in TOOLS
    ]:
        assert set(re.findall(r"\b(\d+(?:\.\d+)?) eur\b", flat(text))) <= {"8"}
    coordinator = flat(registry.get(COORDINATOR).instructions)
    assert "never infer capital from a scheduler's total_amount_quote" in coordinator
    assert "native ledger is the only capital authority" in coordinator


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
        assert "before the close" in text
        assert "0.15 percentage points" in text
    coordinator = flat(registry.get(COORDINATOR).instructions)
    assert "15 to 25 percent of bot equity" in coordinator
    assert "review every held stock on every tick" in coordinator
    assert "an exit is a trade too" in coordinator
    reviewer = flat(registry.get(REVIEWER).instructions)
    assert "1.5 times the preview's estimated round-trip cost" in reviewer
    assert "at least 0.8%" in reviewer
    assert "0.15 percentage points above the preview's round-trip cost" in reviewer
    assert "closed session" in reviewer
    analyst = flat(registry.get(ANALYST).instructions)
    assert "at least 0.8%" in analyst and "1.5:1" in analyst


def test_entries_prefer_eight_euro_and_pass_when_the_cost_guard_blocks(registry):
    loop = StrategyStore().get(COORDINATOR, "intraday")
    coordinator = flat(registry.get(COORDINATOR).instructions)
    text = flat(loop.instructions)
    for prompt in (coordinator, text):
        assert "prefer entries of at least 8 eur" in prompt
        assert "cost_guard_blocks_buy" in prompt
        assert "pass" in prompt
    assert "never try to get around that refusal" in coordinator
    assert "a sale of a held stock is never blocked by it" in coordinator
    reviewer = flat(registry.get(REVIEWER).instructions)
    assert "cost_guard_blocks_buy" in reviewer
    assert "never rejected for cost or for being small" in reviewer


def test_the_daily_brake_is_five_percent_below_the_new_york_day_opening_equity(
    registry,
):
    loop = StrategyStore().get(COORDINATOR, "intraday")
    for text in (
        flat(registry.get(COORDINATOR).instructions),
        flat(loop.instructions),
    ):
        assert "5% below" in text
        assert "new york trading day" in text
        assert "opening equity" in text
        assert "exits stay allowed" in text


def test_exits_are_never_blocked_on_incomplete_paperwork(registry):
    loop = StrategyStore().get(COORDINATOR, "intraday")
    coordinator = flat(registry.get(COORDINATOR).instructions)
    text = flat(loop.instructions)
    assert "exits are never blocked on paperwork" in coordinator
    assert "if the reviewer returns incomplete twice" in coordinator
    assert "escalate by re-asking with the missing bounds filled in" in coordinator
    assert "only an approve" in text
    assert "never place an exit without an approve" in coordinator
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
        assert "changed stock, side or amount" in text


def test_prices_are_labelled_reference_and_the_real_fill_comes_from_trading_212(
    registry,
):
    loop = StrategyStore().get(COORDINATOR, "intraday")
    coordinator = flat(registry.get(COORDINATOR).instructions)
    text = flat(loop.instructions)
    for prompt in (coordinator, text):
        assert "reference" in prompt
        assert "kraken xstocks public data" in prompt
        assert "real fill price" in prompt
    assert "trading 212 has no quote endpoint" in coordinator
    assert "trust those over any reference price" in coordinator
    assert "not trading 212's own quote" in text
    analyst = flat(registry.get(ANALYST).instructions)
    assert "trading 212 has no quote endpoint" in analyst
    assert "reference prices in usd" in analyst
    reviewer = flat(registry.get(REVIEWER).instructions)
    assert "reference prices in usd" in reviewer
    assert "tokenized copy" in reviewer


def test_the_order_is_a_day_limit_cancelled_by_the_service_one_per_call(registry):
    coordinator = flat(registry.get(COORDINATOR).instructions)
    text = flat(StrategyStore().get(COORDINATOR, "intraday").instructions)
    for prompt in (coordinator, text):
        assert "day limit order priced through the reference price" in prompt
        assert "bounded wait" in prompt
        assert 'order_type="market"' in prompt
        assert "definitely refuses limit orders" in prompt
        assert "keep the same size" in prompt
        assert "one order per call" in prompt
        assert "never retry" in prompt or "never retried" in prompt
    assert "a zero or partial fill is a normal" in coordinator
    assert "pending is not a fill" in coordinator
    assert "reconcile" in coordinator and "reconcile" in text


def test_the_crew_trades_only_us_stocks_in_a_eur_account_inside_the_session(registry):
    loop = StrategyStore().get(COORDINATOR, "intraday")
    coordinator = flat(registry.get(COORDINATOR).instructions)
    text = flat(loop.instructions)
    assert "only us stocks that trading 212 lists" in coordinator
    assert "never trade another asset class or instrument" in coordinator
    assert "09:30 to 16:00 new york" in coordinator
    assert "never treat usd as eur" in coordinator
    # a closed session ends the tick before any scan, consultation or order
    first_step = text[text.index("1. call") : text.index("2. scan")]
    assert "closed" in first_step and "end the tick" in first_step
    assert "no scan, no analyst, no order" in first_step
    assert "do not treat usd as eur" in flat(registry.get(REVIEWER).instructions)
    assert "never equate usd with eur" in flat(registry.get(ANALYST).instructions)
    assert "us stocks listed on trading 212" in flat(
        registry.get(REVIEWER).instructions
    )
    assert "short selling" in coordinator and "short selling" in text
    # no 24/7 or crypto framing leaks in from the other crews
    for slug in TOOLS:
        instructions = flat(registry.get(slug).instructions)
        assert "around the clock" not in instructions
        assert "crypto pairs" not in instructions


def test_nothing_in_the_crew_names_another_venues_tool(registry):
    for slug in TOOLS:
        text = registry.get(slug).instructions
        assert not re.search(r"\bcrypto_com_exchange\b", text), slug
        assert not re.search(r"\brevolut_x\b", text), slug
        assert not re.search(r"\b(?:get_prices|get_market_data|run_code)\b", text), slug
    loop = StrategyStore().get(COORDINATOR, "intraday").instructions
    assert not re.search(r"\b(?:crypto_com_exchange|revolut_x)\b", loop)


def test_the_crew_never_touches_keys_or_credentials(registry):
    for slug in TOOLS:
        text = flat(registry.get(slug).instructions)
        assert "credentials" in text
    coordinator = flat(registry.get(COORDINATOR).instructions)
    assert "change the api key or secret" in coordinator
    assert "never send credentials" in coordinator


def _tool_node():
    tree = ast.parse(
        (ROOT / "mcp_servers/condor/server.py").read_text(encoding="utf-8")
    )
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "trading212"
    )


def test_native_tool_exposes_no_enable_credentials_sync_or_money_limit_mutation():
    tool = _tool_node()
    actions = set(ast.literal_eval(tool.args.args[0].annotation.slice))
    assert actions == {
        "status",
        "valuation",
        "accounts",
        "history",
        "stocks",
        "candles",
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
        "order_type",
        "timeframe",
        "count",
        "keyword",
        "verdict",
        "target_pct",
        "stop_pct",
        "horizon_hours",
        "note",
    }
    order_type = next(a for a in tool.args.args if a.arg == "order_type")
    assert set(ast.literal_eval(order_type.annotation.slice)) == {"limit", "market"}


def test_the_native_tool_never_offers_a_way_around_the_service_guards():
    server = (ROOT / "mcp_servers/condor/server.py").read_text(encoding="utf-8")
    assert "allow_costly_buy" not in server
    # the route does not offer the override either: a refused buy stays refused
    route = (ROOT / "condor/web/routes/trading212.py").read_text(encoding="utf-8")
    assert "allow_costly_buy" not in route


@pytest.mark.parametrize("name", ["trading212", "mcp__condor__trading212"])
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
        "stocks",
        "candles",
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
