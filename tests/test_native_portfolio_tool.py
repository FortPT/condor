"""The chat agent's way to see the Crypto.com, Revolut X and Trading 212 money.

Hummingbot's ``get_portfolio_overview`` cannot see these accounts, so the Condor
chat answered "your portfolio is empty ($0.00)". ``native_portfolio`` is a
read-only tool on the condor MCP server, backed by the same code path as the
dashboard (``condor.native_accounts``).
"""

import asyncio
from pathlib import Path

import pytest

from condor import native_accounts
from condor.runtime.danger import is_dangerous_tool_call, is_mutating_tool_call
from condor.web.models import WebUser
from condor.web.routes import portfolio as portfolio_route
from mcp_servers.condor import profiles
from mcp_servers.condor import server as condor_server
from mcp_servers.hummingbot_api import profiles as hb_profiles

ROOT = Path(__file__).resolve().parent.parent


def test_the_tool_calls_the_read_only_native_endpoint(monkeypatch):
    calls = []

    async def fake_call(method, path, body=None, timeout=None):
        calls.append((method, path, body))
        return {"venues": [], "total_usd": 0.0}

    monkeypatch.setattr("mcp_servers.condor.condor_client.call_main_api", fake_call)
    result = asyncio.run(condor_server.native_portfolio())
    assert result == {"venues": [], "total_usd": 0.0}
    assert calls == [("GET", "/portfolio/native", None)]


def test_the_endpoint_reads_the_callers_own_accounts(monkeypatch):
    seen = []

    async def fake_overview(user_id):
        seen.append(user_id)
        return {"venues": [], "total_usd": 0.0, "notes": []}

    monkeypatch.setattr(native_accounts, "overview", fake_overview)
    out = asyncio.run(portfolio_route.get_native_portfolio(WebUser(id=42, role="user")))
    assert out["total_usd"] == 0.0
    assert seen == [42]


@pytest.mark.parametrize("profile", ["tick", "agent", "full"])
def test_every_seat_that_mounts_the_condor_server_gets_the_tool(profile):
    assert "native_portfolio" in profiles.PROFILE_TOOLS[profile]
    assert "native_portfolio" in profiles.TOOL_DESCRIPTIONS


def test_the_tool_is_neither_gated_nor_logged_as_a_change():
    call = {"tool": "mcp__condor__native_portfolio", "input": {}}
    assert not is_dangerous_tool_call(call)
    assert not is_mutating_tool_call(call)


def test_the_hummingbot_switch_line_says_its_scope():
    line = hb_profiles.TOOL_DESCRIPTIONS["get_portfolio_overview"]
    assert "Hummingbot-connected accounts only" in line


def test_the_condor_chat_agent_is_told_about_both_tools():
    prompt = (ROOT / "agents" / "condor" / "AGENT.md").read_text()
    assert "`native_portfolio`" in prompt
    assert "Hummingbot-connected accounts only" in prompt
    assert "Crypto.com" in prompt and "Trading 212" in prompt
