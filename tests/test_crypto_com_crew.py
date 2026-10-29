"""Native crew discovery, actual MCP boundaries and authored review policy.

These tests do not run models or trade. Review is a prompt contract; money and
quote guards remain the existing native service's separately tested boundary.
"""

import ast
import re
from pathlib import Path

import pytest

from condor.agents.agent import AgentStore
from condor.agents.strategy import StrategyStore
from condor.runtime.toolsets import allowlist_mutes, every_tool_name

ROOT = Path(__file__).resolve().parents[1]
COORDINATOR = "crypto_com_exchange_trader"
ANALYST = "crypto_com_market_analyst"
REVIEWER = "crypto_com_risk_reviewer"
TOOLS = {
    COORDINATOR: {
        "crypto_com_exchange",
        "delegate",
        "get_market_data",
        "get_prices",
        "trading_agent_journal_read",
        "trading_agent_journal_write",
        "manage_memory",
        "send_notification",
    },
    ANALYST: {"get_prices", "get_market_data", "trading_agent_journal_read"},
    REVIEWER: {"trading_agent_journal_read"},
}


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


def test_only_coordinator_owns_a_loop_and_existing_scheduler_defaults_survive(registry):
    loops = StrategyStore()
    assert loops.list(ANALYST) == []
    assert loops.list(REVIEWER) == []
    loop = loops.get(COORDINATOR, "daily_spot")
    assert loop is not None
    assert loop.default_config == {
        "frequency_sec": 300,
        "tick_timeout_sec": 600,
        "execution_mode": "loop",
        "total_amount_quote": 30,
        "restart_on_boot": True,
        "max_ticks": 0,
    }
    assert "native ledger" in loop.instructions
    assert "explicit owner start" in loop.instructions


def test_consultation_references_resolve_and_market_precedes_bounded_intent_review(
    registry,
):
    loop = StrategyStore().get(COORDINATOR, "daily_spot")
    for text in (registry.get(COORDINATOR).instructions, loop.instructions):
        targets = re.findall(r'delegate\(action="ask", agent="([^"]+)"', text)
        assert targets == [ANALYST, REVIEWER]
        assert all(registry.get(slug) is not None for slug in targets)
        assert text.index(ANALYST) < text.index(REVIEWER)
        assert "sequential" in text
        assert "bounded" in text and "preview" in text
    assert (
        loop.instructions.index('action="preview"')
        < loop.instructions.index(f'agent="{REVIEWER}"')
        < loop.instructions.index('action="order"')
    )


def test_authored_protocol_denies_missing_review_and_checks_every_final_quote_bound(
    registry,
):
    coordinator = registry.get(COORDINATOR).instructions.lower()
    reviewer = registry.get(REVIEWER).instructions.lower()
    loop = StrategyStore().get(COORDINATOR, "daily_spot").instructions.lower()
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
    assert "no quotation on the exchange" in reviewer
    assert "out-of-bounds" in reviewer and "expired" in reviewer
    assert (
        loop.index('agent="crypto_com_risk_reviewer"')
        < loop.index("take one fresh preview")
        < loop.index('action="order"')
    )
    for slug in TOOLS:
        instructions = registry.get(slug).instructions
        assert not re.search(r"EUR (?:30|26|5)\b", instructions)
        assert "cumulative losses" in instructions


def test_native_tool_exposes_no_enable_credentials_or_money_limit_mutation():
    tree = ast.parse(
        (ROOT / "mcp_servers/condor/server.py").read_text(encoding="utf-8")
    )
    tool = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "crypto_com_exchange"
    )
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
        "maker_stats",
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
        "mode",
        "purpose",
        "maker_wait_sec",
    }


def _flat(text):
    """Lowercase with line wraps collapsed, so a phrase may span a wrap in the prompt."""
    return " ".join(text.lower().split())


def _both(registry):
    loop = StrategyStore().get(COORDINATOR, "daily_spot").instructions
    return {
        "agent": registry.get(COORDINATOR).instructions,
        "loop": loop,
    }


def test_cash_rotation_rules_are_in_the_agent_and_the_loop(registry):
    for where, text in _both(registry).items():
        flat = _flat(text)
        for phrase in (
            "cash rotation",
            "cash is below the minimum entry",
            "sell the weakest eligible holding, then buy the candidate",
            "ignore dust below 1",
            "never a holding that is above its entry-implied target path",
            "still within its thesis window",
            "smallest remaining expected move",
            "thesis is invalidated or stale",
            "each from a fresh preview",
            "times the usual cost multiple (1.3)",
            "plus the holding's own forgone expected move",
            "one rotation at a time, at most one rotation per 3 hours",
            "none while the 5% daily brake is hit",
            "each has its own bounded intent",
            "approved by the reviewer",
            "its own fresh preview is inside the approved bounds",
            "do not chase",
            "no second attempt, no substitute coin",
            "the cash simply stays cash",
            "a stop or invalidation exit is never held up by any of this",
        ):
            assert phrase in flat, (where, phrase)
        # The old, unbounded permission is gone: a rotation is never free-form.
        assert "is allowed when the new coin's expected net gain beats" not in flat
        assert "follows the cash rotation rules below" in flat


def test_cheap_idle_ticks_say_when_they_apply_and_what_they_still_check(registry):
    for where, text in _both(registry).items():
        flat = _flat(text)
        for phrase in (
            "cheap idle tick",
            "status is connected, enabled and not uncertain",
            "cash is below the minimum entry (5 in the quote currency)",
            "bid minus invalidation is at most 25% of entry minus invalidation",
            "none has reached its target",
            "less than half of the target made",
            "not cheap-safe, so run the normal tick",
            "less than 60 minutes ago",
            "at least once per hour the tick is a full tick",
            "no analyst, no reviewer, no previews, no scorecard calls",
            "no candle or book reads",
            "compare every held coin's bid with its invalidation level and target",
            "the 5% daily brake",
            "write one journal line",
            "stop being cheap and continue as a full tick",
            "a cheap tick itself stays quiet",
            "one log per tick is waived for cheap idle ticks only",
            "at least one call, so the paper test continues",
            "do not change the loop's frequency",
        ):
            assert phrase in flat, (where, phrase)


def test_the_loop_checks_the_cheap_tick_rule_before_step_one_and_scopes_the_scorecard(
    registry,
):
    loop = StrategyStore().get(COORDINATOR, "daily_spot").instructions
    assert loop.index("cheap idle tick rule") < loop.index("\n1. Call")
    assert "after the analyst's report each full tick" in loop
    # The new paragraphs follow the order step; they add no second consultation.
    assert loop.index('action="order"') < loop.index("Cash rotation")
    assert loop.index("Cash rotation") < loop.index("Cheap idle ticks")


def test_rotation_and_cheap_ticks_leave_the_review_protocol_and_cadence_alone(registry):
    for text in _both(registry).values():
        # Still exactly one analyst and one reviewer consultation call, in order.
        assert re.findall(r'delegate\(action="ask", agent="([^"]+)"', text) == [
            ANALYST,
            REVIEWER,
        ]
        assert "never enable" in text.lower()
        assert "cumulative losses" in text
        assert not re.search(r"EUR (?:30|26|5)\b", text)
    loop = StrategyStore().get(COORDINATOR, "daily_spot")
    # The cadence is configured outside the prompt and is untouched.
    assert loop.default_config["frequency_sec"] == 300
    assert "frequency_sec" not in loop.instructions


def test_the_rotation_and_cheap_tick_paragraphs_match_in_agent_and_loop(registry):
    def section(text, start, end):
        return _flat(text[text.index(start) : text.index(end)])

    both = _both(registry)
    assert section(both["agent"], "Cash rotation", "Cheap idle ticks") == section(
        both["loop"], "Cash rotation", "Cheap idle ticks"
    )
    assert section(both["agent"], "Cheap idle ticks", "\x00") if False else True
    tail_agent = _flat(both["agent"][both["agent"].index("Cheap idle ticks") :])
    tail_loop = _flat(
        both["loop"][
            both["loop"]
            .index("Cheap idle ticks") : both["loop"]
            .index("Authoring or discovering")
        ]
    )
    assert tail_agent == tail_loop


def test_maker_mode_is_optional_bounded_and_never_used_for_stops(registry):
    for where, text in _both(registry).items():
        flat = _flat(text)
        for phrase in (
            "maker mode (owner instruction 2026-10-29, optional)",
            "it may never fill",
            "nothing has run live yet",
            "the owner switches maker mode on",
            "when it is false use taker mode as before",
            'use mode="maker" only for an entry (purpose="entry", a buy)',
            'a target exit (purpose="target_exit", a sell)',
            "the thesis window is at least 1 hour",
            "maker_taker_round_trip_pct (a maker entry plus a taker exit)",
            "times the usual cost multiple (1.3) is at most the target",
            "the 1.6% target bar is unchanged",
            "maker mode lowers the cost, it does not lower the bar",
            "always use taker mode for a stop or invalidation exit",
            "for a rotation leg and for any exit that must complete",
            "the effective price, which is the resting price at the touch, never through it",
            "at most 75",
            "an unfilled order changes nothing: no trade happened",
            "never retry an unfilled or partial maker order as a taker order in the same tick",
            "never resubmit it, and never chase the price",
            "a post-only refusal (the price would cross the book) is a refusal",
            "journal the maker outcome (filled, partial or unfilled)",
            'action="maker_stats"',
            "never change the mode or the cost bar from those numbers yourself",
        ):
            assert phrase in flat, (where, phrase)
    # The maker paragraph sits before the rotation and idle-tick rules and adds no
    # consultation, so the order of the protocol and those sections is unchanged.
    for text in _both(registry).values():
        assert text.index("Maker mode (owner") < text.index("Cash rotation (owner")
        assert re.findall(r'delegate\(action="ask", agent="([^"]+)"', text) == [
            ANALYST,
            REVIEWER,
        ]


def test_the_cheap_idle_tick_never_reads_maker_stats(registry):
    for text in _both(registry).values():
        idle = _flat(text[text.index("Cheap idle ticks") :])
        assert "maker_stats" not in idle
        assert "no analyst, no reviewer, no previews, no scorecard calls" in idle


def test_the_reviewer_binds_maker_intents_and_the_analyst_flags_patient_entries(
    registry,
):
    reviewer = _flat(registry.get(REVIEWER).instructions)
    for phrase in (
        "a bounded intent may carry mode maker or taker (taker when absent)",
        "purpose entry or target_exit (a stop exit is never maker)",
        "maker_taker_round_trip_pct",
        "at least 0.15 percentage points above it",
        "the same maximum slippage and cost bounds to the effective price",
        "reject an intent whose resting price is above the reviewed ask for a buy",
        "below the reviewed bid for a sell",
        "never a retry as a taker order",
        "missing maker fields mean incomplete",
        "unknown cost basis means incomplete, never approval",
    ):
        assert phrase in reviewer, phrase
    analyst = _flat(registry.get(ANALYST).instructions)
    assert "patient" in analyst and "urgent" in analyst
