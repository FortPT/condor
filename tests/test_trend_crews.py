"""The slow-trend crews follow a code signal: small prompts, no consultations, the same guards."""

from pathlib import Path

import pytest
import yaml

AGENTS = Path(__file__).resolve().parent.parent / "agents"
CREWS = {
    "revolut_x_trend_trader": ("revolut_x", "Revolut X"),
    "crypto_com_trend_trader": ("crypto_com_exchange", "Crypto.com"),
}


def split(path):
    text = path.read_text(encoding="utf-8")
    head = yaml.safe_load(text.split("---")[1])
    return head, " ".join(text.split("---", 2)[2].split())


@pytest.mark.parametrize("slug", sorted(CREWS))
def test_the_agent_can_only_call_its_venue_tool_and_the_journal(slug):
    tool, _ = CREWS[slug]
    head, _ = split(AGENTS / slug / "AGENT.md")
    assert set(head["tools"]) == {
        tool,
        "trading_agent_journal_read",
        "trading_agent_journal_write",
        "manage_memory",
        "send_notification",
    }
    assert "delegate" not in head["tools"]


@pytest.mark.parametrize("slug", sorted(CREWS))
def test_the_prompt_follows_the_code_signal_and_keeps_the_guards(slug):
    tool, _ = CREWS[slug]
    _, text = split(AGENTS / slug / "AGENT.md")
    assert f'{tool}(action="trend")' in text
    assert "Act on the `action` and nothing else" in text
    assert "sell ALL the units" in text and "Exits first" in text
    assert "about 35% of the bot's equity" in text
    assert "never retry an order" in text.lower() or "never retry" in text.lower()
    assert "The native guards" in text and "authoritative" in text
    assert "no_data" in text
    for banned in ("delegate", "analyst to", "predict(", "scorecard("):
        assert banned not in text


def test_the_crypto_com_crew_never_uses_maker_mode_and_leaves_dust_alone():
    _, text = split(AGENTS / "crypto_com_trend_trader" / "AGENT.md")
    assert "never use maker mode" in text
    assert "dust in other coins is not yours to sell" in text
    assert "daily close above its prior 20-day high" in text


def test_the_revolut_x_crew_refuses_a_buy_the_cost_guard_blocks():
    _, text = split(AGENTS / "revolut_x_trend_trader" / "AGENT.md")
    assert "cost_guard_blocks_buy" in text
    assert "above its 50-day average" in text


@pytest.mark.parametrize("slug", sorted(CREWS))
def test_the_loop_runs_every_four_hours_and_starts_nothing(slug):
    head, text = split(AGENTS / slug / "loops" / "daily" / "loop.md")
    cfg = head["default_config"]
    assert cfg["frequency_sec"] == 14400 and cfg["execution_mode"] == "loop"
    assert cfg["restart_on_boot"] is True and cfg["total_amount_quote"] == 0
    assert "no analyst and no reviewer" in head["default_trading_context"]
    assert "starts nothing and changes no financial activation" in text
    assert "`no_data` is never a reason to act" in text
