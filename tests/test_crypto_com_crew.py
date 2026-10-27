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
COORDINATOR = "crypto_com_app_trader"
ANALYST = "crypto_com_market_analyst"
REVIEWER = "crypto_com_risk_reviewer"
TOOLS = {
    COORDINATOR: {
        "crypto_com_app",
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
        "frequency_sec": 900,
        "tick_timeout_sec": 180,
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
        assert "bounded" in text and "quote ID" in text
    assert (
        loop.instructions.index('action="quote"')
        < loop.instructions.index(f'agent="{REVIEWER}"')
        < loop.instructions.index('action="execute"')
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
        for guard in ("no order", "journal", "notify", "fresh final", "new review"):
            assert guard in text
        assert "never enable" in text
        assert "native" in text and "guards" in text
    for field in (
        "quote id",
        "kind",
        "currency",
        "requested source amount",
        "maximum source amount",
        "minimum received amount",
        "costs/spread",
        "utc validity deadline",
        "allocation snapshot",
        "countdown",
    ):
        assert field in reviewer
    assert "unknown cost basis means incomplete, never approval" in reviewer
    assert (
        "indicative quote id as evidence, explicitly not an executable id" in reviewer
    )
    assert "out-of-bounds" in reviewer and "expired" in reviewer
    assert (
        loop.index('agent="crypto_com_risk_reviewer"')
        < loop.index("request one fresh final quote")
        < loop.index('action="execute"')
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
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "crypto_com_app"
    )
    actions = set(ast.literal_eval(tool.args.args[0].annotation.slice))
    assert actions == {
        "status",
        "valuation",
        "accounts",
        "history",
        "coins",
        "quote",
        "execute",
        "reconcile",
        "predict",
        "scorecard",
    }
    assert {arg.arg for arg in tool.args.args} == {
        "action",
        "kind",
        "source",
        "target",
        "amount",
        "quotation_id",
        "keyword",
        "verdict",
        "target_pct",
        "stop_pct",
        "horizon_hours",
        "note",
    }
